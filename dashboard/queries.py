"""Data access for the data-health console.

Every page reads the monitor's own tables, so the dashboard shows exactly what the
scheduled job recorded - no second implementation of any number here. A fresh
read-only connection per query rather than a cached one: the nightly job writes the
database from another process, and a connection opened once at app start would keep
serving yesterday's snapshot to a console whose whole job is to say "today looks
different".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.db import FAMILY_LABELS  # noqa: E402
from monitor.thresholds import MIN_HISTORY, SIGMA  # noqa: E402

DB_PATH = Path(os.environ.get("MONITOR_DUCKDB", ROOT / "db" / "monitor.duckdb"))
INCIDENT_REPORT = ROOT / "reports" / "incident_report.json"

# Findings first, worst first - the same ordering the alert router uses, so the
# console and the page-out agree on what deserves attention.
SEVERITY_RANK = """case severity when 'critical' then 0 when 'warning' then 1 else 2 end"""
STATUS_RANK = """case status when 'fail' then 0 when 'error' then 1
                 when 'warn' then 2 else 3 end"""


def _query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def available() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        return _query("select count(*) n from dq.runs").iloc[0, 0] > 0
    except Exception:
        return False


@st.cache_data(ttl="60s", show_spinner=False)
def runs(limit: int = 60) -> pd.DataFrame:
    return _query(
        f"""
        select run_id, started_at, snapshot_sha, checks_total, passed, warned,
               failed, errored, alerts_sent, wall_ms
        from dq.runs
        order by started_at desc
        limit {int(limit)}
        """
    )


@st.cache_data(ttl="60s", show_spinner=False)
def run_count() -> int:
    return int(_query("select count(*) n from dq.runs").iloc[0, 0])


def latest_run_id() -> str:
    frame = _query("select run_id from dq.runs order by started_at desc limit 1")
    return "" if frame.empty else str(frame.iloc[0, 0])


@st.cache_data(ttl="60s", show_spinner=False)
def run_row(run_id: str) -> pd.DataFrame:
    return _query("select * from dq.runs where run_id = ?", [run_id])


@st.cache_data(ttl="60s", show_spinner=False)
def results(run_id: str) -> pd.DataFrame:
    return _query(
        f"""
        select check_id, family, scope, name, description, severity, status,
               observed, expected, tolerance, unit, detail, duration_ms, drilldown_sql
        from dq.results
        where run_id = ?
        order by {STATUS_RANK}, {SEVERITY_RANK}, check_id
        """,
        [run_id],
    )


@st.cache_data(ttl="60s", show_spinner=False)
def status_by_family(run_id: str) -> pd.DataFrame:
    return _query(
        """
        select family,
               count(*) filter (status = 'fail')   as fails,
               count(*) filter (status = 'error')  as errors,
               count(*) filter (status = 'warn')   as warns,
               count(*) filter (status = 'pass')   as passes,
               count(*)                            as total
        from dq.results
        where run_id = ?
        group by family
        order by fails + errors desc, family
        """,
        [run_id],
    )


@st.cache_data(ttl="60s", show_spinner=False)
def result_history(check_id: str, limit: int = 40) -> pd.DataFrame:
    """One check's observed value across runs - how a drift band earns its keep."""
    return _query(
        f"""
        select r.run_id, r.started_at, q.observed, q.expected, q.status, q.detail
        from dq.results q
        join dq.runs r using (run_id)
        where q.check_id = ?
        order by r.started_at desc
        limit {int(limit)}
        """,
        [check_id],
    )


@st.cache_data(ttl="60s", show_spinner=False)
def one_result(run_id: str, check_id: str) -> pd.DataFrame:
    return _query(
        "select * from dq.results where run_id = ? and check_id = ? limit 1",
        [run_id, check_id],
    )


@st.cache_data(ttl="60s", show_spinner=False)
def drilldown(sql: str, limit: int = 50) -> tuple[pd.DataFrame | None, str]:
    """Run the check's own root-cause query. Returns (frame, error).

    Cached on the SQL text: the query comes from the check registry, not from the
    browser, and a triage session re-selects the same finding repeatedly while it
    digs - each dig should not re-scan the warehouse.
    """
    if not sql or not sql.strip():
        return None, "this check publishes no drill-down query"
    try:
        return _query(f"select * from ({sql}) t limit {int(limit)}"), ""
    except Exception as exc:
        return None, str(exc).splitlines()[0]


# --- learned thresholds ------------------------------------------------------


@st.cache_data(ttl="60s", show_spinner=False)
def baselines() -> pd.DataFrame:
    frame = _query(
        """
        select metric_key, n_runs, mean, sd, min_tol, last_value, updated_at
        from dq.baselines
        order by metric_key
        """
    )
    # Same expression the engine judges with, imported rather than restated: if the
    # dashboard computed its own band, it could show a green band around a red run.
    frame["band"] = (SIGMA * frame["sd"]).clip(lower=frame["min_tol"])
    frame["judging"] = frame["n_runs"] >= MIN_HISTORY
    return frame


@st.cache_data(ttl="60s", show_spinner=False)
def metric_history(metric_key: str) -> pd.DataFrame:
    return _query(
        """
        select m.recorded_at, m.bucket, m.value, r.run_id
        from dq.metrics m
        join dq.runs r using (run_id)
        where m.metric_key = ?
        order by m.recorded_at, m.bucket
        """,
        [metric_key],
    )


# --- alert state -------------------------------------------------------------


@st.cache_data(ttl="60s", show_spinner=False)
def alerts(status: str = "all") -> pd.DataFrame:
    where = "" if status == "all" else "where status = ?"
    params = [] if status == "all" else [status]
    return _query(
        f"""
        select fingerprint, check_id, scope, severity, status, first_seen, last_seen,
               occurrences, notifications, last_notified_at, runs_since_notify,
               subject, body
        from dq.alerts
        {where}
        order by case status when 'open' then 0 else 1 end, last_seen desc
        """,
        params,
    )


# --- landing provenance ------------------------------------------------------


@st.cache_data(ttl="60s", show_spinner=False)
def manifest() -> pd.DataFrame:
    return _query(
        """
        select source_file, rows, bytes, sha256, loaded_at, run_id
        from raw._load_manifest
        order by source_file
        """
    )


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family, str(family).capitalize())


def incident_report() -> dict | None:
    if not INCIDENT_REPORT.exists():
        return None
    return json.loads(INCIDENT_REPORT.read_text(encoding="utf-8"))
