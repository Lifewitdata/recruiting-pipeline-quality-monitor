"""Run the registry, judge every check, persist what was found.

The engine knows nothing about recruiting: it executes each check's SQL, applies
that check's comparison rule, and writes a row the dashboard and the alert router
both read. Keeping the judgement in one place is what lets a new check be a data
class instance instead of a code path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from monitor.checks.base import ERROR, FAIL, PASS, WARN, Check, Outcome
from monitor.thresholds import LearnedThresholds

EPS = 1e-9


@dataclass
class RunReport:
    run_id: str
    outcomes: list[Outcome] = field(default_factory=list)
    started_at: object | None = None
    finished_at: object | None = None
    wall_ms: int = 0
    snapshot_sha: str = ""

    def by_status(self) -> dict[str, int]:
        counts = {PASS: 0, WARN: 0, FAIL: 0, ERROR: 0}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        return counts

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status in (FAIL, ERROR)]

    @property
    def critical_failures(self) -> list[Outcome]:
        return [o for o in self.failures if o.check.severity == "critical"]


def _band_outcome(delta: float, tolerance: float, fail_beyond: float) -> str:
    if delta <= tolerance + EPS:
        return PASS
    if delta <= tolerance * fail_beyond + EPS:
        return WARN
    return FAIL


def evaluate(con, check: Check, thresholds: LearnedThresholds, run_id: str) -> Outcome:
    started = time.perf_counter()
    try:
        row = con.execute(check.sql).fetchone()
    except Exception as exc:  # a broken check must not take the run down
        return Outcome(
            check=check,
            status=ERROR,
            observed=None,
            expected=check.expected,
            tolerance=check.tolerance,
            detail=f"query failed: {type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    duration_ms = int((time.perf_counter() - started) * 1000)

    observed = None if row is None or row[0] is None else float(row[0])
    expected = float(row[1]) if row is not None and len(row) > 1 and row[1] is not None else check.expected
    detail = row[2] if row is not None and len(row) > 2 and row[2] is not None else None
    tolerance = float(row[3]) if row is not None and len(row) > 3 and row[3] is not None else check.tolerance

    if check.kind == "info":
        status = PASS if observed is not None else ERROR
        if detail is None and observed is not None:
            detail = f"measured {observed:,.4g} {check.unit}"
    elif observed is None:
        status, detail = ERROR, (detail or "check returned no row")
    elif check.kind in ("zero", "equal"):
        status = PASS if abs(observed - (expected or 0.0)) < EPS else FAIL
        if detail is None:
            detail = f"observed {observed:,.4g} vs required {expected or 0.0:,.4g}"
    elif check.kind == "lte":
        status = PASS if observed <= (expected or 0.0) + EPS else FAIL
        if detail is None:
            detail = f"observed {observed:,.4g}, ceiling {expected or 0.0:,.4g}"
    elif check.kind == "close":
        if expected is None or tolerance is None:
            status, detail = ERROR, (detail or "close check without expected + tolerance")
        else:
            status = _band_outcome(abs(observed - expected), tolerance, check.fail_beyond)
            if detail is None:
                delta = observed - expected
                detail = (
                    f"observed {observed:,.4g} vs expected {expected:,.4g} "
                    f"(delta {delta:+,.4g}, band +/-{tolerance:g})"
                )
    elif check.kind == "drift":
        baseline = thresholds.baseline(check.metric_key or check.id, check.tolerance or 0.0)
        thresholds.record(run_id, check.metric_key or check.id, observed, check.tolerance or 0.0)
        if baseline is None or not baseline.usable:
            status = PASS
            n = 0 if baseline is None else baseline.n_runs
            detail = f"seeding baseline - {n} prior run(s), needs 3 before this band means anything"
            tolerance = check.tolerance
        else:
            band = baseline.tolerance
            tolerance = band
            status = _band_outcome(abs(observed - baseline.mean), band, check.fail_beyond)
            expected = baseline.mean
            if detail is None:
                detail = (
                    f"observed {observed:,.4g} vs learned mean {baseline.mean:,.4g} "
                    f"(sd {baseline.sd:,.4g} over {baseline.n_runs} runs, band +/-{band:g})"
                )
    else:  # pragma: no cover - guarded by Check.__post_init__
        status, detail = ERROR, f"unknown kind {check.kind}"

    return Outcome(
        check=check,
        status=status,
        observed=observed,
        expected=expected,
        tolerance=tolerance,
        detail=detail or "",
        duration_ms=duration_ms,
    )


def snapshot_sha(con) -> str:
    rows = con.execute(
        "select source_file, sha256 from raw._load_manifest order by source_file"
    ).fetchall()
    return "|".join(f"{f}:{s[:12]}" for f, s in rows)


def persist(con, report: RunReport, alerts_sent: int = 0) -> None:
    counts = report.by_status()
    con.execute("delete from dq.results where run_id = ?", [report.run_id])
    con.executemany(
        """
        insert into dq.results
            (result_id, run_id, checked_at, check_id, family, scope, name, description,
             severity, status, observed, expected, unit, tolerance, detail,
             drilldown_sql, duration_ms)
        values (nextval('dq.seq_results'), ?, current_timestamp, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            [
                report.run_id,
                o.check.id,
                o.check.family,
                o.check.scope,
                o.check.name,
                o.check.description,
                o.check.severity,
                o.status,
                o.observed,
                o.expected,
                o.check.unit,
                o.tolerance,
                o.detail,
                o.check.drilldown,
                o.duration_ms,
            ]
            for o in report.outcomes
        ],
    )
    con.execute("delete from dq.runs where run_id = ?", [report.run_id])
    con.execute(
        """
        insert into dq.runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            report.run_id,
            report.started_at,
            report.finished_at,
            report.snapshot_sha,
            len(report.outcomes),
            counts[PASS],
            counts[WARN],
            counts[FAIL],
            counts[ERROR],
            alerts_sent,
            report.wall_ms,
        ],
    )


def run(con, checks: list[Check], run_id: str) -> RunReport:
    thresholds = LearnedThresholds(con)
    report = RunReport(run_id=run_id, snapshot_sha=snapshot_sha(con))
    t0 = time.perf_counter()
    for check in checks:
        report.outcomes.append(evaluate(con, check, thresholds, run_id))
    thresholds.refresh()
    report.wall_ms = int((time.perf_counter() - t0) * 1000)
    return report
