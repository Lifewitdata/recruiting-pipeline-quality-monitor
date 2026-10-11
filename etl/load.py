"""Land the CSVs in DuckDB as two layers and record exactly what was landed.

raw  - every column VARCHAR, values exactly as the source delivered them
       (empty strings, float-serialised zipcodes, all of it). Nothing is
       cleaned here, because a check that runs after cleaning can only prove
       the cleaner works, not that the source is sound.
stg  - the same rows with the columns the monitor actually measures cast to
       the types the dataset card declares. Casting is done with try_cast so
       an unparseable value becomes NULL and stays visible to a check instead
       of failing the load.

Every run appends to raw._load_manifest with the file's sha256 and row count,
so any number in a report can be traced back to specific source bytes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etl import fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("MONITOR_DUCKDB", ROOT / "db" / "monitor.duckdb"))

TABLES = ("candidates", "jobs", "history")

# Column -> target type, applied in stg only. Chosen to match the types the
# dataset card publishes, so a card-vs-file claim can be tested, not assumed.
TYPED = {
    "candidates": {
        "create_date": "DATE",
        "actual_salary": "DOUBLE",
        "actual_daily_salary": "DOUBLE",
    },
    "jobs": {
        "create_date": "DATE",
        "salary": "DOUBLE",
        "daily_rate": "DOUBLE",
    },
    "history": {
        "spontaneous_application_date": "DATE",
        "shortlist_date": "DATE",
        "qualification_date": "DATE",
        "resume_sent_to_company_date": "DATE",
        "1st_interview_date": "DATE",
        "2nd_interview_date": "DATE",
        "3rd_interview_date": "DATE",
        "4th_interview_date": "DATE",
        "job_offer_proposed_date": "DATE",
        "job_offer_accepted_date": "DATE",
        "end_of_process_date": "DATE",
    },
}


def q(name: str) -> str:
    """Quote an identifier - several funnel columns start with a digit."""
    return '"' + name.replace('"', '""') + '"'


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for schema in ("raw", "stg", "dq"):
        con.execute(f"create schema if not exists {schema}")
    return con


def _replace_expr(casts: dict[str, str]) -> str:
    inner = ",\n".join(
        f"    try_cast(\"{col}\" as {typ}) as \"{col}\"" for col, typ in casts.items()
    )
    return f"* replace (\n{inner}\n)"


def land(
    con: duckdb.DuckDBPyConnection,
    table: str,
    csv: Path,
    run_id: str,
    digest: str,
    size: int,
) -> dict:
    """Land one CSV into raw + stg and record it in the manifest.

    Split out from `load()` so the incident harness can land a mutated extract
    through the same code path the real pipeline uses - a fixture that takes a
    different route to the warehouse proves nothing about the real one.
    """
    con.execute(f"drop table if exists raw.{table}")
    # all_varchar keeps the landing byte-faithful: empty strings, float
    # serialised zipcodes and all. _row_num is the stable join key between
    # raw and stg, so a check can ask "landed but uncastable" per row.
    con.execute(
        f"""
        create table raw.{table} as
        select row_number() over () as _row_num, r.*, ?::timestamptz as _loaded_at
        from read_csv('{(csv.as_posix())}', all_varchar = true, header = true) r
        """,
        [now()],
    )
    rows = con.execute(f"select count(*) from raw.{table}").fetchone()[0]

    con.execute(f"drop table if exists stg.{table}")
    con.execute(f"create table stg.{table} as select {_replace_expr(TYPED[table])} from raw.{table}")

    con.execute(
        """
        create table if not exists raw._load_manifest (
            run_id     varchar,
            loaded_at  timestamptz,
            source_file varchar,
            sha256     varchar,
            bytes      bigint,
            rows       bigint
        )
        """
    )
    con.execute("delete from raw._load_manifest where source_file = ?", [f"{table}.csv"])
    con.execute(
        "insert into raw._load_manifest values (?, ?, ?, ?, ?, ?)",
        [run_id, now(), f"{table}.csv", digest, size, rows],
    )
    return {"table": table, "rows": rows, "sha256": digest}


def build_event_log(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("drop table if exists stg.events")
    funnel_cols = [s for s in TYPED["history"] if s != "end_of_process_date"]
    con.execute(
        f"""
        create table stg.events as
        select application_id, candidate_id, job_id, stage, stage_date
        from stg.history
        unpivot (stage_date for stage in ({", ".join(q(c) for c in funnel_cols)}))
        """
    )
    return con.execute("select count(*) from stg.events").fetchone()[0]


def load(con: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    loaded = []
    for table in TABLES:
        csv = fetch.DATA_DIR / f"{table}.csv"
        digest, size = fetch.FILES[f"{table}.csv"]
        reason = fetch.check(csv, digest, size)
        if reason is not None:
            raise SystemExit(f"{csv.name}: {reason}\nrun `python etl/fetch.py` first.")

        loaded.append(land(con, table, csv, run_id, digest, size))
        print(f"  landed raw.{table:<11} {loaded[-1]['rows']:>7,} rows   stg.{table} typed")

    events = build_event_log(con)
    print(f"  built stg.events    {events:>7,} events   (one row per populated stage cell)")
    return loaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", default=None, help="defaults to the current UTC timestamp")
    ap.add_argument(
        "--new-run",
        action="store_true",
        help="force a fresh landing even when the manifest already matches (used by tests)",
    )
    args = ap.parse_args()

    if DB_PATH.exists() and not args.new_run:
        print(f"re-landing into existing {DB_PATH.name}")

    con = connect()
    run_id = args.run_id or now().strftime("load-%Y%m%dT%H%M%SZ")
    print(f"run_id={run_id}  db={DB_PATH}")
    loaded = load(con, run_id)
    total = sum(r["rows"] for r in loaded)
    print(f"  total {total:,} rows across {len(loaded)} tables")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
