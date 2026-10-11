"""DuckDB connection and the `dq` schema the monitor writes its own state to.

Everything the monitor learns lives in these tables, which is what makes the
thresholds learned rather than typed into a config file: a run reads the history
previous runs wrote, and a run writes the history the next run will read.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("MONITOR_DUCKDB", ROOT / "db" / "monitor.duckdb"))

DDL = {
    "dq.runs": """
        create table if not exists dq.runs (
            run_id       varchar primary key,
            started_at   timestamptz,
            finished_at  timestamptz,
            snapshot_sha varchar,
            checks_total integer,
            passed       integer,
            warned       integer,
            failed       integer,
            errored      integer,
            alerts_sent  integer,
            wall_ms      integer
        )
    """,
    "dq.results": """
        create table if not exists dq.results (
            result_id     bigint,
            run_id        varchar,
            checked_at    timestamptz,
            check_id      varchar,
            family        varchar,
            scope         varchar,
            name          varchar,
            description   varchar,
            severity      varchar,
            status        varchar,
            observed      double,
            expected      double,
            unit          varchar,
            tolerance     double,
            detail        varchar,
            drilldown_sql varchar,
            duration_ms   integer
        )
    """,
    # One row per (metric, bucket) per run. Cross-run drift reads this table to
    # decide what "normal" was, so a threshold is never a magic number.
    "dq.metrics": """
        create table if not exists dq.metrics (
            run_id     varchar,
            recorded_at timestamptz,
            metric_key varchar,
            bucket     varchar,
            value      double
        )
    """,
    "dq.baselines": """
        create table if not exists dq.baselines (
            metric_key  varchar primary key,
            mean        double,
            sd          double,
            n_runs      integer,
            last_value  double,
            min_tol     double,
            updated_at  timestamptz
        )
    """,
    # Alert state, not alert text: the fingerprint is what lets a check that has
    # been failing for six days produce one alert instead of six hundred. Counters
    # are measured since the last notification, which is what a cooldown needs.
    "dq.alerts": """
        create table if not exists dq.alerts (
            fingerprint        varchar primary key,
            check_id           varchar,
            scope              varchar,
            severity           varchar,
            status             varchar,
            first_seen         timestamptz,
            last_seen          timestamptz,
            occurrences        integer,
            notifications      integer,
            last_notified_at   timestamptz,
            runs_since_notify  integer,
            subject            varchar,
            body              varchar
        )
    """,
    "dq.seq_results": "create sequence if not exists dq.seq_results start 1 increment 1",
}

FAMILY_LABELS = {
    "contract": "Source-of-truth contract",
    "structure": "Key & referential structure",
    "funnel": "Funnel logical consistency",
    "format": "Value format & domain",
    "trend": "Freshness, volume & anomaly",
    "reconcile": "Metric vs source reconciliation",
}


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target), read_only=read_only)
    # The monitor owns `dq`; `raw` and `stg` belong to the loader. Creating the schema
    # here is what lets a fresh checkout run its tests before any data has landed.
    if not read_only:
        con.execute("create schema if not exists dq")
        for statement in DDL.values():
            con.execute(statement)
    return con


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
