"""Fixture builders shared by the monitor's tests.

Held outside `conftest.py` so a test can import them as plain functions - the routing
logic takes a `Check` and an `Outcome`, and the clearest way to test it is to hand it
exactly those.
"""

from __future__ import annotations

from monitor import db as dbmod
from monitor.checks.base import FAIL, Check, Outcome
from monitor.engine import RunReport

# Two rows a drill-down can point at: the trajectory whose offer was accepted with no
# offer proposed, and the one dated outside the published window. Both are real
# findings from the extract; the columns match what the landing table publishes.
HISTORY = """
    create table stg.history as
    select * from (values
        (29160, date '2023-07-17', 'Offer Accepted'),
        (43725, date '2025-05-03', 'Qualification')
    ) as v(application_id, end_of_process_date, last_stage_reached)
"""


def monitor_database(tmp_path):
    """A throwaway DuckDB with the monitor's own schema and a two-row landing table."""
    con = dbmod.connect(tmp_path / "monitor.duckdb")
    con.execute("create schema if not exists stg")
    con.execute(HISTORY)
    return con


def make_check(
    check_id: str = "funnel.accepted_without_proposed",
    severity: str = "critical",
    kind: str = "zero",
    unit: str = "trajectories",
    drilldown: str = (
        "select application_id, end_of_process_date, last_stage_reached "
        "from stg.history where application_id = 29160"
    ),
) -> Check:
    return Check(
        id=check_id,
        family=check_id.split(".")[0],
        scope="history.csv",
        name="an accepted offer always has a proposal behind it",
        severity=severity,
        kind=kind,
        description=(
            "A trajectory that ends in an accepted offer with no recorded proposal is either "
            "a skipped stage in the source or a partial load."
        ),
        sql="select 0",
        unit=unit,
        drilldown=drilldown,
    )


def make_outcome(
    check: Check | None = None,
    status: str = FAIL,
    observed: float | None = 1.0,
    expected: float | None = 0.0,
    detail: str = "observed 1 vs required 0",
) -> Outcome:
    return Outcome(
        check=check or make_check(),
        status=status,
        observed=observed,
        expected=expected,
        tolerance=None,
        detail=detail,
        duration_ms=3,
    )


def make_report(outcomes: list[Outcome], run_id: str = "run-0001") -> RunReport:
    return RunReport(run_id=run_id, outcomes=outcomes, snapshot_sha="candidates.csv:deadbeef")
