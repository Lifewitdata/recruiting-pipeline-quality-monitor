"""Incident injection: prove the monitor catches each fault, and only the fault.

A data-quality suite is a claim about detection, and the claim needs a number
attached to it. Running it on a healthy file proves nothing about the failure mode
it was written for, and running it on the real JTH extract cannot either: every
finding there is a genuine upstream defect, so there is no ground truth to score
against.

This harness supplies the ground truth. It starts from the synthetic fixture
(`etl/synthesize.py`), which satisfies its own dataset card completely, lands it,
runs the suite until the learned thresholds have a real band, then injects exactly
one known fault and re-runs. Detection is measured as "at least one of the checks
this fault should have moved left pass"; the control run measures the false-positive
rate by re-running the untouched fixture and comparing statuses across runs.

Every injected fault is a fault this suite has already seen in the wild on the real
extract, or a classic way a nightly pipeline breaks: a truncated load, a column the
card never documented, a rate the publisher got wrong, a pair that stops being
unique, a monotonic sort that stopped sorting.

Run:  python -m monitor.incidents
      python -m monitor.incidents --only grain_collision --verbose
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from monitor.card import STAGES
from etl import load as loader
from monitor import alerts, card, db, engine
from monitor.checks import build_registry
from monitor.thresholds import MIN_HISTORY

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "synthetic"
REPORT_PATH = ROOT / "reports" / "incident_report.json"
TABLES = ("candidates", "jobs", "history")

# The injected run needs a learned band to be judged against, otherwise a drift
# check reports "seeding" and any finding it makes is unattributable.
WARM_RUNS = MIN_HISTORY


# --- fixture plumbing ---------------------------------------------------------


def connect(path: Path):
    con = loader.connect(path)
    for statement in db.DDL.values():
        con.execute(statement)
    return con


def copy_fixture(work: Path) -> Path:
    data = work / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    for table in TABLES:
        shutil.copyfile(FIXTURE_DIR / f"{table}.csv", data / f"{table}.csv")
    return data


def land_all(con, data: Path, run_id: str) -> None:
    for table in TABLES:
        path = data / f"{table}.csv"
        raw = path.read_bytes()
        loader.land(con, table, path, run_id, hashlib.sha256(raw).hexdigest(), len(raw))
    loader.build_event_log(con)


def registry() -> list:
    return build_registry(card.parse(FIXTURE_DIR / "dataset_card.md"))


# --- CSV editing helpers ------------------------------------------------------


def read_table(data: Path, table: str) -> tuple[list[str], list[dict]]:
    with (data / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_table(data: Path, table: str, columns: list[str], rows: list[dict]) -> None:
    with (data / f"{table}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def edit(data: Path, table: str, fn: Callable[[list[dict]], list[dict]],
         columns: list[str] | None = None) -> None:
    """Rewrite one table of the extract through `fn`."""
    names, rows = read_table(data, table)
    write_table(data, table, columns or names, fn(rows))


def shift(value: str, days: int) -> str:
    return (dt.date.fromisoformat(value) + dt.timedelta(days=days)).isoformat()


# --- the incidents -------------------------------------------------------------


@dataclass(frozen=True)
class Incident:
    id: str
    story: str
    expect: tuple[str, ...]
    must_page: bool
    # a file-level fault mutates the extract before it is landed (the realistic
    # case: the vendor shipped something different); a warehouse-level fault is
    # SQL that runs after the landing, because some breakages live in the model
    # and never touch the source bytes.
    in_file: Callable[[Path], None] | None = None
    in_warehouse: str | None = None
    # what the run taught us that the design did not predict - usually a detector
    # boundary worth writing down instead of quietly re-rolling the expectation
    note: str = ""


def _truncate(rows: list[dict]) -> list[dict]:
    return rows[:-150]


def _add_column(data: Path) -> None:
    def add(rows: list[dict]) -> list[dict]:
        for i, row in enumerate(rows):
            row["screening_score"] = f"{3.2 + (i % 9) * 0.1:.1f}"
        return rows

    names, rows = read_table(data, "history")
    write_table(data, "history", names + ["screening_score"], add(rows))


def _drop_column(data: Path) -> None:
    def cut(rows: list[dict]) -> list[dict]:
        for row in rows:
            row.pop("years_experience", None)
        return rows

    names, rows = read_table(data, "jobs")
    write_table(data, "jobs", [n for n in names if n != "years_experience"], cut(rows))


def _float_serialise(data: Path) -> None:
    """`06` -> `6.0`: what an integer dtype round trip does to a code column."""

    def mangle(rows: list[dict]) -> list[dict]:
        for i, row in enumerate(rows):
            if row["zipcode"] and i % 7 == 0:
                row["zipcode"] = f"{int(row['zipcode'])}.0"
        return rows

    edit(data, "candidates", mangle)


def _skills_go_missing(data: Path) -> None:
    def mangle(rows: list[dict]) -> list[dict]:
        for i, row in enumerate(rows):
            if i % 3 == 0:
                row["skills"] = ""
        return rows

    edit(data, "candidates", mangle)


def _keys_go_null(data: Path) -> None:
    def mangle(rows: list[dict]) -> list[dict]:
        for row in rows[:12]:
            row["candidate_id"] = ""
        return rows

    edit(data, "history", mangle)


def _replayed_delta(data: Path) -> None:
    """The same candidate-job pair arriving twice under a new application id."""

    def mangle(rows: list[dict]) -> list[dict]:
        clones = []
        for n, row in enumerate(rows[:20]):
            again = dict(row)
            again["application_id"] = f"a99{n:04d}"
            clones.append(again)
        return rows + clones

    edit(data, "history", mangle)


def _monotonic_sort_breaks(data: Path) -> None:
    def mangle(rows: list[dict]) -> list[dict]:
        done = 0
        for row in rows:
            if row["1st_interview_date"] and row["2nd_interview_date"] and done < 15:
                row["2nd_interview_date"] = shift(row["1st_interview_date"], -3)
                done += 1
        return rows

    edit(data, "history", mangle)


def _offer_fabricated(data: Path) -> None:
    """An accepted offer with nothing proposed: the one impossible transition."""

    def mangle(rows: list[dict]) -> list[dict]:
        done = 0
        for row in rows:
            if row["job_offer_proposed_date"] and not row["job_offer_accepted_date"] and done < 10:
                row["job_offer_accepted_date"] = row["job_offer_proposed_date"]
                row["job_offer_proposed_date"] = ""
                row["last_stage_reached"] = "Offer Accepted"
                done += 1
        return rows

    edit(data, "history", mangle)


def _pre_window_rows(data: Path) -> None:
    """Whole trajectories dated before the collection window ever opened.

    Only rows with no unsolicited-application date, so the leak is a window
    violation and not also a trajectory older than its own candidate.
    """

    def mangle(rows: list[dict]) -> list[dict]:
        done = 0
        for row in rows:
            if done >= 3:
                break
            if row[STAGES[0]] or not row[STAGES[1]]:
                continue
            for stage in STAGES:
                if row[stage]:
                    row[stage] = shift(row[stage], -1800)
            if row["end_of_process_date"]:
                row["end_of_process_date"] = shift(row["end_of_process_date"], -1800)
            done += 1
        return rows

    edit(data, "history", mangle)


def _date_format_changes(data: Path) -> None:
    """`2024-01-13` -> `13/01/2024`: a regional export lands in the feed.

    Applied to rows that go deeper than shortlist, so the unparseable cell becomes
    a NULL without changing which stage is deepest on the row.
    """

    def mangle(rows: list[dict]) -> list[dict]:
        done = 0
        for row in rows:
            if row["qualification_date"] and row["shortlist_date"] and done < 8:
                y, m, d = row["shortlist_date"].split("-")
                row["shortlist_date"] = f"{d}/{m}/{y}"
                done += 1
        return rows

    edit(data, "history", mangle)


def _unknown_label(data: Path) -> None:
    def mangle(rows: list[dict]) -> list[dict]:
        done = 0
        for row in rows:
            if row["last_stage_reached"] == "3rd Interview" and done < 6:
                row["last_stage_reached"] = "Interview Round 5"
                done += 1
        return rows

    edit(data, "history", mangle)


def _newest_partition_missing(data: Path) -> None:
    """Every row of the newest month gone, except the single pinned newest date.

    One row keeps the calendar reaching December while 137 trajectories disappear,
    so the funnel's own shape - the month that produced nothing - is the only way to
    see it. This is the incident that a row count and a staleness check both wave
    past: the file is 95 % complete and its newest date is today's.
    """

    def mangle(rows: list[dict]) -> list[dict]:
        newest = max(row["end_of_process_date"] for row in rows)
        return [
            row
            for row in rows
            if row["end_of_process_date"][:7] != newest[:7]
            or row["end_of_process_date"] == newest
        ]

    edit(data, "history", mangle)


INCIDENTS: tuple[Incident, ...] = (
    Incident(
        id="truncated_extract",
        story="A job dies halfway through writing history.csv: 150 rows never arrive.",
        expect=(
            "contract.row_count.history",
            "contract.event_count.history",
            "trend.row_count_drift.history",
        ),
        must_page=True,
        in_file=lambda data: edit(data, "history", _truncate),
    ),
    Incident(
        id="unrequested_column",
        story="The ATS enables a scoring field nobody asked for and nobody documented.",
        expect=("contract.columns_undocumented.history",),
        must_page=False,
        in_file=_add_column,
    ),
    Incident(
        id="documented_column_dropped",
        story="jobs.csv stops shipping years_experience, which the card still promises.",
        expect=(
            "contract.columns_missing.jobs",
            "contract.claim_missing.jobs.years_experience",
        ),
        must_page=True,
        in_file=_drop_column,
    ),
    Incident(
        id="float_dtype_round_trip",
        story="A code column passes through a numeric dtype: 06 becomes 6.0 and the leading zero is gone.",
        expect=(
            "format.text_type_fidelity.candidates.zipcode",
            "format.zipcode_canonical.candidates",
            "format.zipcode_leading_zero_lost.candidates",
        ),
        must_page=False,
        in_file=_float_serialise,
    ),
    Incident(
        id="published_rate_drifted",
        story="Extraction stops parsing a third of the skill lists. The card still says 5.3 % are empty.",
        expect=("contract.claim_missing.candidates.skills",),
        must_page=True,
        in_file=_skills_go_missing,
    ),
    Incident(
        id="orphan_foreign_key",
        story="A partial dimension load leaves trajectories pointing at candidates that are not in the file.",
        expect=(
            "structure.fk_null.history.candidate_id",
            "structure.fk_orphan.history.candidate_id",
        ),
        must_page=True,
        in_file=_keys_go_null,
    ),
    Incident(
        id="grain_collision",
        story="Yesterday's delta is re-appended without de-duplicating: the same candidate-job pair twice.",
        expect=("contract.grain.history", "contract.row_count.history"),
        must_page=True,
        in_file=_replayed_delta,
    ),
    Incident(
        id="monotonic_sort_breaks",
        story="The jitter that is supposed to preserve stage order runs on one row and does not.",
        expect=("funnel.stage_order",),
        must_page=True,
        in_file=_monotonic_sort_breaks,
    ),
    Incident(
        id="offer_never_proposed",
        story="A placement recorded as accepted with no offer ever proposed - the transition that is physically impossible.",
        expect=("funnel.accepted_without_proposed",),
        must_page=True,
        in_file=_offer_fabricated,
    ),
    Incident(
        id="pre_window_rows",
        story="Three trajectories arrive dated before the published collection window opened.",
        expect=("funnel.date_window",),
        must_page=False,
        in_file=_pre_window_rows,
    ),
    Incident(
        id="date_format_changes",
        story="A regional export writes shortlist dates as DD/MM/YYYY; the loader casts them to NULL and the funnel quietly loses the stage.",
        expect=("contract.date_parse.history.shortlist_date",),
        must_page=True,
        in_file=_date_format_changes,
    ),
    Incident(
        id="new_stage_label",
        story="A new value appears in last_stage_reached: a schema change arriving through a data column.",
        expect=(
            "funnel.label_vocabulary",
            "funnel.label_conformance",
            "reconcile.label_partition",
        ),
        must_page=True,
        in_file=_unknown_label,
    ),
    Incident(
        id="newest_partition_missing",
        story="A whole month of trajectories never arrived. One late row survives, so the extract still looks current.",
        expect=(
            "trend.volume_last_month.events",
            "contract.row_count.history",
            "trend.row_count_drift.history",
        ),
        must_page=True,
        in_file=_newest_partition_missing,
        note=(
            "row count and business recency both stay inside their bands here: the "
            "file is 95% complete and its newest date is the newest anywhere in the "
            "snapshot. Only the series shape - one month producing almost nothing - "
            "sees this, which is why the trailing-median ratio is kept next to the "
            "z-score instead of replacing it."
        ),
    ),
    Incident(
        id="event_log_drops_a_stage",
        story="An incremental model change deletes one stage from the event log. The wide table is untouched, so nothing else notices.",
        expect=(
            "reconcile.stage_population.3rd_interview",
            "reconcile.event_log_completeness",
        ),
        must_page=True,
        in_warehouse="delete from stg.events where stage = '3rd_interview_date'",
    ),
)


# --- running them --------------------------------------------------------------


@dataclass
class Finding:
    check_id: str
    status: str
    severity: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "severity": self.severity,
            "detail": self.detail,
        }


def _findings(report) -> list[Finding]:
    return [
        Finding(o.check.id, o.status, o.check.severity, o.detail)
        for o in report.outcomes
        if o.status != engine.PASS
    ]


def run_incident(incident: Incident, work: Path, checks: list) -> dict:
    data = copy_fixture(work)
    con = connect(work / "monitor.duckdb")
    try:
        land_all(con, data, f"{incident.id}-warm")
        for i in range(WARM_RUNS):
            engine.run(con, checks, f"{incident.id}-warm-{i}")

        if incident.in_file is not None:
            incident.in_file(data)
            land_all(con, data, f"{incident.id}-inject")
        else:
            con.execute(incident.in_warehouse)
        injected = engine.run(con, checks, f"{incident.id}-injected")
        routing = alerts.route(con, injected, alerts.Policy())

        findings = {f.check_id: f for f in _findings(injected)}
        detected = [cid for cid in incident.expect if cid in findings]
        missed = [cid for cid in incident.expect if cid not in findings]
        unattributed = [f for cid, f in findings.items() if cid not in incident.expect]
        pages = [d.message.subject for d in routing.to_send]
        return {
            "id": incident.id,
            "story": incident.story,
            "layer": "source file" if incident.in_file else "warehouse model",
            "expected": list(incident.expect),
            "detected": detected,
            "missed": missed,
            "detected_share": round(100.0 * len(detected) / len(incident.expect), 1),
            "unattributed": [f.as_dict() for f in unattributed],
            "should_page": incident.must_page,
            "paged": len(pages),
            "page_subjects": pages,
            "note": incident.note,
            "findings": [f.as_dict() for f in findings.values()],
        }
    finally:
        con.close()


def control(runs: int, work: Path, checks: list) -> dict:
    """Repeat the untouched fixture and watch for statuses that move on their own."""
    data = copy_fixture(work)
    con = connect(work / "monitor.duckdb")
    try:
        land_all(con, data, "control")
        statuses: dict[str, list[str]] = {}
        findings: list[dict] = []
        for i in range(runs):
            report = engine.run(con, checks, f"control-{i}")
            for o in report.outcomes:
                statuses.setdefault(o.check.id, []).append(o.status)
                if o.status != engine.PASS:
                    findings.append(
                        Finding(o.check.id, o.status, o.check.severity, o.detail).as_dict()
                    )
        # The first MIN_HISTORY runs report "seeding" instead of judging, so the
        # false-positive figure is only meaningful after the bands exist.
        judged = {cid: seen[WARM_RUNS:] for cid, seen in statuses.items()}
        unstable = {cid: seen for cid, seen in judged.items() if len(set(seen)) > 1}
        return {
            "runs": runs,
            "judged_runs": max(runs - WARM_RUNS, 0),
            "checks": len(checks),
            "findings": findings,
            "flapping_checks": sorted(unstable),
            "false_positive_rate": round(
                100.0 * len({f["check_id"] for f in findings}) / len(checks), 2
            ),
        }
    finally:
        con.close()


def run(only: list[str] | None, keep: Path | None, verbose: bool) -> dict:
    checks = registry()
    base = keep or Path(tempfile.mkdtemp(prefix="dq-incidents-"))
    print(f"registry: {len(checks)} checks   warm runs per incident: {WARM_RUNS}")
    print(f"workdir:  {base}\n")

    clean = control(WARM_RUNS + 2, base / "control", checks)
    print(
        f"control   {clean['runs']} runs on the untouched fixture: "
        f"{len(clean['findings'])} finding(s), "
        f"{len(clean['flapping_checks'])} flapping check(s)"
    )

    selected = [i for i in INCIDENTS if not only or i.id in only]
    results = []
    for incident in selected:
        result = run_incident(incident, base / incident.id, checks)
        results.append(result)
        mark = "OK " if not result["missed"] and (result["paged"] or not incident.must_page) else "MISS"
        print(
            f"{mark:<5} {incident.id:<27} {len(result['detected'])}/{len(incident.expect)} "
            f"expected check(s) fired, {result['paged']} page(s), "
            f"{len(result['unattributed'])} other finding(s)"
        )
        if verbose:
            print(f"      {incident.story}")
            for f in result["findings"]:
                print(f"      [{f['status']}/{f['severity']}] {f['check_id']}: {f['detail'][:140]}")
            for subject in result["page_subjects"]:
                print(f"      page -> {subject}")
            if incident.note:
                print(f"      note: {incident.note}")

    detected = sum(len(r["detected"]) for r in results)
    expected = sum(len(r["expected"]) for r in results)
    by_id = {i.id: i for i in selected}
    silent = [r["id"] for r in results if by_id[r["id"]].must_page and not r["paged"]]
    over_paged = [r["id"] for r in results if not by_id[r["id"]].must_page and r["paged"]]
    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fixture": str(FIXTURE_DIR.relative_to(ROOT)),
        "checks_in_registry": len(checks),
        "warm_runs_per_incident": WARM_RUNS,
        "incidents": len(results),
        "incidents_fully_detected": sum(1 for r in results if not r["missed"]),
        "checks_expected": expected,
        "checks_detected": detected,
        "detection_rate_pct": round(100.0 * detected / expected, 2),
        "criticals_paged": sum(r["paged"] for r in results),
        "incidents_that_paged": sum(1 for r in results if r["paged"]),
        "incidents_needing_a_page": sum(1 for i in selected if i.must_page),
        "incidents_that_should_not_page_but_did": over_paged,
        "incidents_that_should_page_but_did_not": silent,
        "control": clean,
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"\ndetection {detected}/{expected} designed checks "
        f"({summary['detection_rate_pct']}%), "
        f"{summary['incidents_fully_detected']}/{len(results)} incidents fully attributed"
    )
    print(
        f"paging    {summary['criticals_paged']} alert(s) routed across "
        f"{summary['incidents_that_paged']} incident(s); "
        f"{len(silent)} silent, {len(over_paged)} over-eager"
    )
    print(f"report    {REPORT_PATH}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="*", help="run just these incident ids")
    ap.add_argument("--keep", help="work in this directory instead of a temp one")
    ap.add_argument("--verbose", action="store_true", help="print every finding per incident")
    args = ap.parse_args()

    summary = run(args.only, Path(args.keep) if args.keep else None, args.verbose)
    failed = [r["id"] for r in summary["results"] if r["missed"]]
    silent = summary["incidents_that_should_page_but_did_not"]
    unclean = summary["control"]["findings"]
    if failed or silent or unclean:
        print(f"undetected={failed} silent={silent} control_findings={len(unclean)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
