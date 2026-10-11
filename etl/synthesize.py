"""A synthetic recruiting extract that satisfies its own dataset card, byte-identical
on every machine.

Why this exists: the monitor's job is to compare a file against the claims published
about it, so testing the monitor needs *both* sides - and the real JTH extract cannot
be committed (third-party personal data, CC BY-NC). Worse, every finding on the real
extract is a genuine upstream defect, so a test suite that asserted "all checks pass"
would be asserting something untrue.

The fixture solves both. It is generated with no RNG - pure index arithmetic plus a
CRC32 for the missing-value pattern - so the same 2,000 / 240 / 3,000 rows appear on
every machine, and the card's numbers are *measured from the generated rows* before
being written down. That makes the clean baseline provably green, which is what lets
the incident harness (`monitor/incidents.py`) attribute every finding to the fault it
injected and to nothing else.

Run:  python etl/synthesize.py              # writes tests/fixtures/synthetic/
      python etl/synthesize.py --out DIR
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor.card import STAGES  # noqa: E402
from monitor.checks.funnel import LABELS  # noqa: E402

OUT_DIR = ROOT / "tests" / "fixtures" / "synthetic"

N_CANDIDATES = 2_000
N_JOBS = 240
N_TRAJECTORIES = 3_000
MONTH_START = dt.date(2023, 1, 1)
N_MONTHS = 24
LAST_DAY = dt.date(2024, 12, 28)
# Trajectories start in the second month: an entity pool that is empty in month 0
# would force either a dangling foreign key or a timestamp older than its own
# candidate, and `funnel.applied_before_entity_existed` is a critical check.
HISTORY_FIRST_MONTH = 1

# Rows whose deepest populated stage is the stage at this index. Strictly decreasing
# from shortlist onwards: a funnel that widens at a later step is the defect
# `reconcile.funnel_chain_monotone` looks for, so the fixture must not contain one.
DEEPEST_COUNTS = [400, 520, 380, 300, 280, 240, 200, 160, 120, 400]

COLUMNS = {
    "candidates": [
        "candidate_id", "create_date", "skills", "expertise_area", "job_category",
        "years_experience", "actual_salary", "actual_daily_salary", "contract_type",
        "zipcode", "source", "llm_seniority_level", "llm_highest_diploma",
    ],
    "jobs": [
        "job_id", "create_date", "expertise_area", "job_category", "skills",
        "contract_type", "years_experience", "zipcode", "salary", "daily_rate",
        "source", "llm_seniority_level", "llm_is_startup",
    ],
    "history": ["application_id", "candidate_id", "job_id", *STAGES,
                "end_of_process_date", "last_stage_reached", "application_source"],
}

EXPERTISE = ["data;backend", "frontend", "cloud;devops", "mobile", "qa;testing", "fullstack"]
CATEGORY = [
    "data engineer;analytics engineer",
    "backend developer",
    "devops engineer",
    "frontend developer",
    "qa engineer",
    "mobile developer",
]
SKILLS = [
    "python;sql;dbt",
    "javascript;react;typescript",
    "ansible;kubernetes;terraform",
    "java;spring",
    "_rare_skill_;prolog",
    "azure;data factory",
    "go;kubernetes",
]
CONTRACT_CANDIDATE = ["permanent", "freelance", "permanent;freelance", "fixed-term;permanent"]
CONTRACT_JOB = ["permanent", "freelance", "fixed-term"]
# The card bins this attribute differently per table (`0-2 years` there, `0-2` here),
# and the fixture honours each table's own published domain. On the real extract this
# asymmetry is what makes 398 jobs rows leave their documented bins.
YEARS_CANDIDATE = ["0-2 years", "3-6 years", "+6 years"]
YEARS_JOB = ["0-2", "3-6", "+6 years"]
DEPARTEMENTS = ["06", "13", "31", "33", "44", "59", "69", "75", "84", "92", "94"]
SOURCE = ["linkedin", "indeed", "referral", "careers_page", "hubspot"]
SENIORITY = ["Junior", "Mid", "Senior"]
DIPLOMA = ["Bachelor", "Master", "PhD"]

# (table, column) -> 1 row in `stride` is missing. Every column listed here is also
# given a measured rate in the card's Missing-Value Rates table, which is what keeps
# it out of the not-null contract: a column the card says is 17 % empty is not a
# column the file is required to fill.
SPARSITY = {
    ("candidates", "expertise_area"): 6,
    ("candidates", "job_category"): 9,
    ("candidates", "skills"): 18,
    ("candidates", "years_experience"): 3,
    ("candidates", "actual_salary"): 9,
    ("candidates", "actual_daily_salary"): 5,
    ("candidates", "contract_type"): 30,
    ("candidates", "zipcode"): 7,
    ("jobs", "expertise_area"): 5,
    ("jobs", "job_category"): 4,
    ("jobs", "skills"): 25,
    ("jobs", "years_experience"): 8,
    ("jobs", "salary"): 6,
    ("jobs", "daily_rate"): 4,
    ("jobs", "contract_type"): 20,
    ("jobs", "zipcode"): 34,
}

# The card's Missing-Value Rates attributes, in the order the real card publishes
# them. Everything here must exist in monitor.card.CLAIM_COLUMN.
RATE_ATTRIBUTES = [
    "create_date", "skills", "expertise_area", "job_category", "years_experience",
    "salary", "daily_rate", "contract_type", "zipcode",
]
RATE_COLUMN = {
    "create_date": ("create_date", "create_date"),
    "skills": ("skills", "skills"),
    "expertise_area": ("expertise_area", "expertise_area"),
    "job_category": ("job_category", "job_category"),
    "years_experience": ("years_experience", "years_experience"),
    "salary": ("actual_salary", "salary"),
    "daily_rate": ("actual_daily_salary", "daily_rate"),
    "contract_type": ("contract_type", "contract_type"),
    "zipcode": ("zipcode", "zipcode"),
}


def month_start(m: int) -> dt.date:
    year, month_number = divmod(MONTH_START.year * 12 + MONTH_START.month - 1 + m, 12)
    return dt.date(year, month_number + 1, 1)


def day_in(m: int, day: int) -> dt.date:
    return month_start(m).replace(day=min(28, max(1, day)))


def spread(total: int, buckets: int) -> list[int]:
    """Near-uniform integer split with a deterministic 8% wobble, no RNG.

    Monthly volumes cannot be perfectly flat or the anomaly detectors have nothing to
    learn about; they cannot be random or the fixture stops being reproducible.
    """
    weights = [1.0 + 0.08 * math.sin(2 * math.pi * i / 7.0) for i in range(buckets)]
    exact = [total * w / sum(weights) for w in weights]
    counts = [int(v) for v in exact]
    short = total - sum(counts)
    order = sorted(range(buckets), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in range(short):
        counts[order[i % buckets]] += 1
    return counts


def _sparse(table: str, column: str, index: int) -> bool:
    """Is this cell missing? A CRC32 rather than a modulus on the row index.

    `index % stride` would line the holes up with the value vocabularies, which cycle
    on the same index, and silently delete whole categories instead of sampling rows.
    """
    stride = SPARSITY.get((table, column))
    if not stride:
        return False
    return zlib.crc32(f"{table}.{column}.{index}".encode()) % stride == 0


def _cell(table: str, column: str, index: int, value):
    return "" if _sparse(table, column, index) else value


def candidates_rows() -> list[dict]:
    rows = []
    index = 0
    for m, count in enumerate(spread(N_CANDIDATES, N_MONTHS)):
        for _ in range(count):
            i = index
            freelance = CONTRACT_CANDIDATE[i % 4] == "freelance"
            salary = 0.0 if i % 97 == 0 else 32_000 + (i * 137) % 45_000
            rows.append({
                "candidate_id": f"c{i:05d}",
                "create_date": day_in(m, (i % 27) + 1).isoformat(),
                "skills": _cell("candidates", "skills", i, SKILLS[i % 7]),
                "expertise_area": _cell("candidates", "expertise_area", i, EXPERTISE[i % 6]),
                "job_category": _cell("candidates", "job_category", i, CATEGORY[i % 6]),
                "years_experience": _cell("candidates", "years_experience", i, YEARS_CANDIDATE[i % 3]),
                "actual_salary": _cell("candidates", "actual_salary", i, f"{salary:.1f}"),
                "actual_daily_salary": _cell(
                    "candidates", "actual_daily_salary", i,
                    f"{480 + (i % 13) * 25:.1f}" if freelance else "0.0",
                ),
                "contract_type": _cell("candidates", "contract_type", i, CONTRACT_CANDIDATE[i % 4]),
                "zipcode": _cell("candidates", "zipcode", i, DEPARTEMENTS[i % 11]),
                "source": SOURCE[i % 5],
                "llm_seniority_level": SENIORITY[i % 3],
                "llm_highest_diploma": DIPLOMA[i % 3],
            })
            index += 1
    rows[-1]["create_date"] = LAST_DAY.isoformat()
    return rows


def jobs_rows() -> list[dict]:
    rows = []
    index = 0
    for m, count in enumerate(spread(N_JOBS, N_MONTHS)):
        for _ in range(count):
            i = index
            freelance = CONTRACT_JOB[i % 3] == "freelance"
            rows.append({
                "job_id": f"j{i:04d}",
                "create_date": day_in(m, (i % 27) + 1).isoformat(),
                "expertise_area": _cell("jobs", "expertise_area", i, EXPERTISE[i % 6]),
                "job_category": _cell("jobs", "job_category", i, CATEGORY[i % 6]),
                "skills": _cell("jobs", "skills", i, SKILLS[(i * 3) % 7]),
                "contract_type": _cell("jobs", "contract_type", i, CONTRACT_JOB[i % 3]),
                "years_experience": _cell("jobs", "years_experience", i, YEARS_JOB[i % 3]),
                "zipcode": _cell("jobs", "zipcode", i, DEPARTEMENTS[(i * 5) % 11]),
                "salary": _cell("jobs", "salary", i, f"{38_000 + (i * 211) % 40_000:.1f}"),
                "daily_rate": _cell(
                    "jobs", "daily_rate", i, f"{520 + (i % 9) * 30:.1f}" if freelance else "0.0"
                ),
                "source": SOURCE[(i * 2) % 5],
                "llm_seniority_level": SENIORITY[(i * 2) % 3],
                "llm_is_startup": "False" if i % 4 == 0 else "True",
            })
            index += 1
    rows[-1]["create_date"] = LAST_DAY.isoformat()
    return rows


def history_rows(candidates: list[dict], jobs: list[dict]) -> list[dict]:
    """One row per candidate-job pair, contiguous stages, label matching the dates."""
    per_month = spread(N_TRAJECTORIES, N_MONTHS - HISTORY_FIRST_MONTH)
    ranks = [rank for rank, n in enumerate(DEEPEST_COUNTS) for _ in range(n)]
    assert len(ranks) == N_TRAJECTORIES, len(ranks)
    # A golden-ratio stride: 1853/3000 is the closest coprime fraction to 0.618, so
    # any contiguous block of trajectory indices samples the depth distribution
    # evenly. A small stride would hand each month one contiguous slice of it, which
    # makes funnel depth correlate with calendar month and trips the anomaly checks.
    ordered = [ranks[(i * 1853) % N_TRAJECTORIES] for i in range(N_TRAJECTORIES)]

    used: set[tuple[int, int]] = set()
    rows: list[dict] = []
    index = 0
    for offset, count in enumerate(per_month):
        m = offset + HISTORY_FIRST_MONTH
        pool_c, pool_j = _pool(candidates, m), _pool(jobs, m)
        for _ in range(count):
            rows.append(_trajectory(index, m, ordered[index], pool_c, pool_j, used))
            index += 1
    _pin_newest(rows)
    assert len(rows) == N_TRAJECTORIES
    return rows


def _pool(entities: list[dict], m: int) -> list[int]:
    """Indices of entities created before month m, oldest first.

    Strictly before, not "up to the 28th of": a trajectory dated on the 3rd would
    otherwise be allowed to reference a profile created on the 28th of the same
    month, and `funnel.applied_before_entity_existed` (critical) measures exactly
    that.
    """
    start = month_start(m)
    return [i for i, e in enumerate(entities) if dt.date.fromisoformat(e["create_date"]) < start]


def _trajectory(i: int, m: int, deepest: int, pool_c: list[int], pool_j: list[int], used) -> dict:
    # Stage 0 is "unsolicited application", not "application": in the real extract
    # most funnel entries begin at shortlist with no value in it. Rows bottoming out
    # at stage 0 are the ones that only ever sent that cold application.
    spontaneous = deepest == 0 or i % 5 == 0
    first = 0 if spontaneous else 1
    base = (i % 20) + 1
    step = (28 - base) // max(deepest - first, 1)

    row = {
        "application_id": f"a{i:06d}",
        "candidate_id": "",
        "job_id": "",
        **{stage: "" for stage in STAGES},
        "end_of_process_date": "",
        "last_stage_reached": LABELS[deepest],
        "application_source": "",
    }
    c, j = _free_pair(i, pool_c, pool_j, used)
    used.add((c, j))
    row["candidate_id"], row["job_id"] = f"c{c:05d}", f"j{j:04d}"

    for k in range(first, deepest + 1):
        row[STAGES[k]] = day_in(m, base + (k - first) * step).isoformat()
    if row[STAGES[0]]:
        row["application_source"] = SOURCE[(i // 5) % 5]
    deepest_day = base + (deepest - first) * step
    row["end_of_process_date"] = day_in(m, deepest_day + 2).isoformat()
    return row


def _free_pair(i: int, pool_c: list[int], pool_j: list[int], used) -> tuple[int, int]:
    """A (candidate, job) pair this file has not used yet.

    The card states one row per candidate-job pair, so `contract.grain.history`
    (critical, expects zero duplicate pairs) is a property the fixture must hold by
    construction rather than by luck.
    """
    ci, ji = (i * 13) % len(pool_c), (i * 7) % len(pool_j)
    for _ in range(len(pool_c)):
        for _ in range(len(pool_j)):
            c, j = pool_c[ci], pool_j[ji]
            if (c, j) not in used:
                return c, j
            ji = (ji + 1) % len(pool_j)
        ci, ji = (ci + 1) % len(pool_c), (i * 7) % len(pool_j)
    raise AssertionError(f"no free candidate-job pair for trajectory {i}")


def _pin_newest(rows: list[dict]) -> None:
    """Give one trajectory the same newest date as both entity tables.

    `trend.data_recency.history` compares each table's newest business date against
    the newest anywhere in the snapshot; without this the fixture's own shape would
    read as a three-week-stale history table.
    """
    for row in reversed(rows):
        if dt.date.fromisoformat(row["end_of_process_date"]) < LAST_DAY:
            row["end_of_process_date"] = LAST_DAY.isoformat()
            return


def build() -> dict[str, list[dict]]:
    candidates = candidates_rows()
    jobs = jobs_rows()
    return {
        "candidates": candidates,
        "jobs": jobs,
        "history": history_rows(candidates, jobs),
    }


def measure(tables: dict[str, list[dict]]) -> dict:
    """Every number the generated card will publish, read back out of the rows."""
    events = sum(
        1 for row in tables["history"] for stage in STAGES if row[stage]
    )
    rates = {}
    for attribute in RATE_ATTRIBUTES:
        column_c, column_j = RATE_COLUMN[attribute]
        rates[attribute] = {
            table: round(
                100.0 * sum(1 for r in rows if not r[column]) / len(rows), 1
            )
            for table, column, rows in (
                ("candidates", column_c, tables["candidates"]),
                ("jobs", column_j, tables["jobs"]),
            )
        }
    return {
        "counts": {
            "candidates": len(tables["candidates"]),
            "jobs": len(tables["jobs"]),
            "trajectories": len(tables["history"]),
            "events": events,
        },
        "rates": rates,
    }


def _row_line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def card_text(measured: dict) -> str:
    counts = measured["counts"]
    lines: list[str] = []
    add = lines.append

    add("**SYNTHETIC JTH-shaped fixture v1.0 - Dataset Card**")
    add("")
    add("Generated by `etl/synthesize.py`. Not a real extract: no person, company or")
    add("salary in this file ever existed. Its purpose is to be a file whose every")
    add("published number is true, so the monitor has a green baseline to break.")
    add("")
    add("---")
    add("")
    add("### • Overview")
    add("")
    add("A recruiting corpus in the shape of JTH, for testing a data-quality monitor.")
    add("")
    add(
        f"It links **{counts['candidates']:,} candidates**, **{counts['jobs']:,} vacancies** and "
        f"**{counts['trajectories']:,} interaction trajectories** (comprising "
        f"**{counts['events']:,} specific events**) collected by a fictional agency "
        f"({month_label()}). Each entity carries the attributes the real card documents, "
        "at day granularity, with the same sparsity pattern reproduced from measured "
        "row counts rather than asserted."
    )
    add("")
    add("---")
    add("")
    add("### • Files & Sizes (CSV UTF-8)")
    add("")
    add(_row_line(["file", "rows", "size", "description"]))
    add(_row_line(["-" * 18, "-" * 6, "-" * 7, "-" * 45]))
    for table, note in (
        ("candidates", "static profile of each applicant"),
        ("jobs", "static profile of each vacancy"),
        ("history", "full, ordered timeline of every candidate-job process"),
    ):
        rows = counts["trajectories"] if table == "history" else counts[table]
        add(_row_line([f"`{table}.csv`", f"{rows:,}", "generated", note]))
    add("")
    add("---")
    add("")
    add("### • Schema")
    add("")

    schema = {
        "candidates": [
            ("`candidate_id`", "`str`", "deterministic hash"),
            ("`create_date`", "`YYYY-MM-DD`", "profile creation date"),
            ("`skills`", "`str`", "semicolon list of manual skills; `_rare_skill_` sentinel for infrequent ones"),
            ("`expertise_area`", "`str`", "manual area(s) of expertise; semicolon list"),
            ("`job_category`", "`str`", "manual job category(ies); semicolon list"),
            ("`years_experience`", "`str` or `NaN`", "binned: `0-2 years`, `3-6 years`, `+6 years`"),
            ("`actual_salary`", "`float` €", "manually entered; µ-aggregated + Laplace noise"),
            ("`actual_daily_salary`", "`float` €", "same as above"),
            ("`contract_type`", "`str`", "semicolon list (e.g. `permanent;freelance`)"),
            ("`zipcode`", "`str`", "French département code (2-digit, coarsened)"),
            ("`source`", "`str`", "acquisition channel, anonymized"),
            ("`llm_seniority_level`", "`str`", "`Junior`, `Mid`, `Senior`"),
            ("`llm_highest_diploma`", "`str`", "e.g. `Bachelor`, `Master`, `PhD`"),
        ],
        "jobs": [
            ("`job_id`", "`str`", "deterministic hash"),
            ("`create_date`", "`YYYY-MM-DD`", "job creation date"),
            ("`expertise_area`", "`str`", "manual expertise area(s); semicolon list"),
            ("`job_category`", "`str`", "manual job category(ies); semicolon list"),
            ("`skills`", "`str`", "manual skills list; `_rare_skill_` sentinel"),
            ("`contract_type`", "`str`", "e.g. `permanent`, `freelance`"),
            ("`years_experience`", "`str` or `NaN`", "binned: `0-2`, `3-6`, `+6 years`"),
            ("`zipcode`", "`str`", "département code; may be empty"),
            ("`salary`", "`float` €", "manually entered"),
            ("`daily_rate`", "`float` €", "manually entered"),
            ("`source`", "`str`", "anonymized source code"),
            ("`llm_seniority_level`", "`str`", "e.g. `Mid`, `Senior`"),
            ("`llm_is_startup`", "`bool`", "startup flag"),
        ],
    }
    header = _row_line(["column", "type", "notes"])
    rule = _row_line(["-" * 30, "-" * 18, "-" * 40])
    for table in ("candidates", "jobs"):
        add(f"#### `{table}.csv`")
        add("")
        add(header)
        add(rule)
        for cells in schema[table]:
            add(_row_line(list(cells)))
        add("")

    add("#### `history.csv`")
    add("")
    add("Each row = one candidate-job pair.")
    add("")
    add(header)
    add(rule)
    add(_row_line(["`application_id`", "`str`", "one row per trajectory"]))
    add(_row_line(["`candidate_id` / `job_id`", "`str`", "foreign keys"]))
    add(_row_line([
        f"`{STAGES[0]}` ... `{STAGES[7]}`", "`YYYY-MM-DD` \\| `NaN`",
        "chronological funnel steps",
    ]))
    add(_row_line([
        f"`{STAGES[8]}` / `{STAGES[9]}`", "`YYYY-MM-DD` \\| `NaN`", "final outcome",
    ]))
    add(_row_line(["`end_of_process_date`", "`YYYY-MM-DD`", "last recorded action"]))
    add(_row_line(["`last_stage_reached`", "`str`", "label of deepest non-null stage"]))
    add(_row_line(["`application_source`", "`str` \\| `NaN`", "channel, present only on unsolicited applications"]))
    add("")
    add("*All dates day-granularity; every funnel date falls inside the published window.*")
    add("")
    add("---")
    add("")
    add("### • Missing-Value Rates")
    add("")
    add(_row_line(["attribute", "candidates", "jobs"]))
    add(_row_line(["-" * 20, "-" * 10, "-" * 10]))
    for attribute in RATE_ATTRIBUTES:
        sides = measured["rates"][attribute]
        add(_row_line([
            f"`{attribute}`", f"{sides['candidates']:.1f} %", f"{sides['jobs']:.1f} %",
        ]))
    add("")
    add("*Rates are measured from the generated rows, not declared in advance: this is")
    add("the one part of a dataset card that a monitor can reconcile against bytes.*")
    add("")
    add("---")
    add("")
    return "\n".join(lines) + "\n"


def month_label() -> str:
    return f"{MONTH_START.strftime('%B')} {MONTH_START.year} - {LAST_DAY.strftime('%B')} {LAST_DAY.year}"


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write(out: Path = OUT_DIR) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tables = build()
    measured = measure(tables)

    manifest = {"generator": "etl/synthesize.py", "measured": measured, "files": {}}
    rows_key = {"candidates": "candidates", "jobs": "jobs", "history": "trajectories"}
    for table in ("candidates", "jobs", "history"):
        path = out / f"{table}.csv"
        write_csv(path, tables[table], COLUMNS[table])
        raw = path.read_bytes()
        manifest["files"][f"{table}.csv"] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "rows": measured["counts"][rows_key[table]],
        }
    card = out / "dataset_card.md"
    card.write_text(card_text(measured), encoding="utf-8")
    manifest["card"] = card.name
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(OUT_DIR), help="directory to write the fixture into")
    args = ap.parse_args()

    manifest = write(Path(args.out))
    counts = manifest["measured"]["counts"]
    print(f"fixture -> {args.out}")
    for name, info in sorted(manifest["files"].items()):
        print(f"  {name:<15} {info['rows']:>7,} rows  {info['bytes']:>9,} bytes  {info['sha256'][:16]}")
    print(
        f"  claims: {counts['candidates']:,} candidates, {counts['jobs']:,} vacancies, "
        f"{counts['trajectories']:,} trajectories, {counts['events']:,} events"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
