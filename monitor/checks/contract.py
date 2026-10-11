"""Contract checks: does the file agree with what the publisher said it contains?

The dataset card publishes row counts, an event count, a column list, a grain
statement and a missing-value rate per attribute. Every expectation in this module
is read out of that card at build time, so the checks test the document against
the data instead of restating it.

This is the family that found the four published rates that the files contradict -
and, just as importantly, the eleven that reconcile to under a percentage point.
"""

from __future__ import annotations

from monitor.card import CLAIM_COLUMN, STAGES, TABLES, parse
from monitor.checks.base import Check, col

# Loader-added bookkeeping columns, never a contract violation when undocumented.
LOADER_COLUMNS = ("_loaded_at", "_row_num")

# A rate published as "27 %" carries a half-point rounding band. Outside it is a
# rounding-direction slip (warning); past four bands it is materially wrong (fail).
CLAIM_TOLERANCE_PP = 0.5
CLAIM_FAIL_BANDS = 4.0


def _is_missing(column: str) -> str:
    """NULL or empty string. The cast keeps it valid on DATE/DOUBLE columns."""
    return f"{col(column)} is null or cast({col(column)} as varchar) = ''"


def _null_pct(table: str, column: str) -> str:
    return f"""
        select round(100.0 * count(*) filter (where {_is_missing(column)})
                       / count(*), 4) as observed
        from stg.{table}
    """


def _documented_values(columns: list[str]) -> str:
    return ", ".join(f"('{c}')" for c in columns)


def build(claims: dict | None = None) -> list[Check]:
    claims = claims or parse()
    checks: list[Check] = []

    # --- 1. row counts -------------------------------------------------------
    for table, expected_rows in claims["file_rows"].items():
        checks.append(
            Check(
                id=f"contract.row_count.{table}",
                family="contract",
                scope=f"{table}.csv",
                name=f"row count matches the card for {table}.csv",
                severity="critical",
                kind="equal",
                unit="rows",
                description=(
                    f"The card's Files & Sizes table claims {expected_rows:,} rows for "
                    f"{table}.csv. A silent row-count change is the most common way an "
                    "upstream extract breaks a downstream number without breaking a build."
                ),
                sql=f"select count(*)::double from stg.{table}",
                expected=float(expected_rows),
                drilldown=(
                    f"select m.source_file, m.rows as landed_rows, m.sha256, m.loaded_at "
                    f"from raw._load_manifest m where m.source_file = '{table}.csv'"
                ),
                reference="dataset_card.md - Files & Sizes",
            )
        )

    # --- 2. event count ------------------------------------------------------
    events = claims["counts"].get("events")
    if events:
        stage_cells = " + ".join(
            f"count(*) filter (where {col(s)} is not null)"
            for s in STAGES
        )
        checks.append(
            Check(
                id="contract.event_count.history",
                family="contract",
                scope="history.csv",
                name="populated funnel cells equal the card's event count",
                severity="critical",
                kind="equal",
                unit="events",
                description=(
                    f"The abstract claims {events:,} specific events. JTH has no separate "
                    "event file - one event is one populated stage timestamp on one "
                    "trajectory - so the claim is only testable by summing the ten stage "
                    "columns. That sum is also the denominator most funnel metrics use."
                ),
                sql=f"select ({stage_cells})::double from stg.history",
                expected=float(events),
                drilldown=" union all ".join(
                    f"select '{s}' as stage, count(*) filter (where {col(s)} is not null)"
                    f"::double as populated from stg.history"
                    for s in STAGES
                ),
                reference="dataset_card.md - abstract",
            )
        )

    # --- 3. published grain --------------------------------------------------
    checks.append(
        Check(
            id="contract.grain.history",
            family="contract",
            scope="history.csv",
            name="one row per candidate-job pair, as the card states",
            severity="warning",
            kind="zero",
            unit="duplicate pairs",
            description=(
                "The card's history section states \"Each row = one candidate-job pair\". "
                "If that held, joining on (candidate_id, job_id) would be safe. Counted "
                "pairs appearing on more than one row instead."
            ),
            sql="""
                select count(*)::double from (
                    select candidate_id, job_id from stg.history
                    group by 1, 2 having count(*) > 1
                )
            """,
            expected=0.0,
            drilldown="""
                select candidate_id, job_id, count(*) as rows_per_pair,
                       min(application_id) as first_application_id,
                       max(application_id) as last_application_id
                from stg.history
                group by 1, 2 having count(*) > 1
                order by rows_per_pair desc, candidate_id
                limit 25
            """,
            reference="dataset_card.md - history.csv grain statement",
        )
    )

    # --- 4. documented vs delivered columns ---------------------------------
    for table in TABLES:
        documented = claims["columns"].get(table, [])
        if not documented:
            continue
        vals = _documented_values(documented)
        allowed = ", ".join(f"'{c}'" for c in LOADER_COLUMNS)
        checks.append(
            Check(
                id=f"contract.columns_undocumented.{table}",
                family="contract",
                scope=f"{table}.csv",
                name=f"every {table}.csv column is documented",
                severity="warning",
                kind="zero",
                unit="columns",
                description=(
                    "Undocumented columns are the ones a downstream analyst finds by "
                    "accident and then trusts without knowing how they were produced."
                ),
                sql=f"""
                    select count(*)::double
                    from information_schema.columns
                    where table_schema = 'stg' and table_name = '{table}'
                      and column_name not in (select c from (values {vals}) as t(c))
                      and column_name not in ({allowed})
                """,
                expected=0.0,
                drilldown=f"""
                    select column_name as undocumented_column, data_type
                    from information_schema.columns
                    where table_schema = 'stg' and table_name = '{table}'
                      and column_name not in (select c from (values {vals}) as t(c))
                      and column_name not in ({allowed})
                    order by 1
                """,
                reference="dataset_card.md - Schema",
            )
        )
        checks.append(
            Check(
                id=f"contract.columns_missing.{table}",
                family="contract",
                scope=f"{table}.csv",
                name=f"every documented {table}.csv column is present",
                severity="critical",
                kind="zero",
                unit="columns",
                description=(
                    "The reverse direction: a documented column that is not in the file "
                    "breaks every query that references it by name."
                ),
                sql=f"""
                    select count(*)::double
                    from (values {vals}) as t(c)
                    where c not in (
                        select column_name from information_schema.columns
                        where table_schema = 'stg' and table_name = '{table}'
                    )
                """,
                expected=0.0,
                reference="dataset_card.md - Schema",
            )
        )

    # --- 5. nullability claims ----------------------------------------------
    # A column whose declared type carries no NaN and that the card gives no
    # missing-rate allowance for is documented as effectively required.
    # llm_* columns are skipped on purpose: they are Llama 3.3 output that the
    # card itself describes as carrying inference noise, so a null in one of them
    # is not a pipeline fault and nobody would action an alert about it.
    rates = claims["missing_rates"]
    for table in TABLES:
        for column, declared in claims["types"].get(table, {}).items():
            if column.startswith("llm_"):
                continue
            if "NaN" in declared:
                continue
            if any(column == CLAIM_COLUMN.get(a, {}).get(table) for a in rates):
                continue
            checks.append(
                Check(
                    id=f"contract.not_null.{table}.{column}",
                    family="contract",
                    scope=f"{table}.csv",
                    name=f"{column} is populated, as its declared type promises",
                    severity="warning",
                    kind="zero",
                    unit="rows",
                    description=(
                        f"The card types {column} as {declared.strip()} with no NaN "
                        "allowance and publishes no missing-value rate for it, which "
                        "documents the column as effectively required."
                    ),
                    sql=_null_count_sql(table, column),
                    expected=0.0,
                    drilldown=_missing_rows_sql(table, column),
                    reference="dataset_card.md - Schema type declaration",
                )
            )

    # --- 6. declared date types actually parse -------------------------------
    for table in TABLES:
        for column, declared in claims["types"].get(table, {}).items():
            if "YYYY-MM-DD" not in declared:
                continue
            checks.append(
                Check(
                    id=f"contract.date_parse.{table}.{column}",
                    family="contract",
                    scope=f"{table}.csv",
                    name=f"{column} values parse as YYYY-MM-DD",
                    severity="critical",
                    kind="zero",
                    unit="unparseable values",
                    description=(
                        "Landing casts every value with try_cast, so anything that does "
                        "not parse becomes NULL and would otherwise be silently counted "
                        "as 'not at this stage' by every funnel metric downstream."
                    ),
                    sql=_unparseable_sql(table, column),
                    expected=0.0,
                    drilldown=_unparseable_rows_sql(table, column),
                    reference="dataset_card.md - declared date type",
                )
            )

    # --- 7. publisher missing-value rates, reconciled ------------------------
    for attribute, sides in rates.items():
        for table, claimed_pct in sides.items():
            column = CLAIM_COLUMN[attribute][table]
            checks.append(
                Check(
                    id=f"contract.claim_missing.{table}.{attribute}",
                    family="contract",
                    scope=f"{table}.csv",
                    name=f"card's missing-value rate for {table}.{attribute}",
                    severity="critical",
                    kind="close",
                    unit="% null",
                    description=(
                        f"The card's Missing-Value Rates table claims {claimed_pct:g} % of "
                        f"{table} rows have no {attribute} (file column "
                        f"{column!r}). Null rates are what a coverage claim on any "
                        "dashboard rests on, so they get reconciled against the bytes."
                    ),
                    sql=_null_pct(table, column),
                    expected=claimed_pct,
                    tolerance=CLAIM_TOLERANCE_PP,
                    fail_beyond=CLAIM_FAIL_BANDS,
                    drilldown=_missing_breakdown_sql(table, column),
                    reference="dataset_card.md - Missing-Value Rates",
                    tags=("claim-reconciliation",),
                )
            )
    return checks


def _null_count_sql(table: str, column: str) -> str:
    return f"select count(*) filter (where {_is_missing(column)})::double from stg.{table}"


def _unparseable_sql(table: str, column: str) -> str:
    return (
        f"select count(*) filter (where r.{col(column)} is not null"
        f"   and trim(r.{col(column)}) <> ''"
        f"   and s.{col(column)} is null)::double "
        f"from raw.{table} r join stg.{table} s on r._row_num = s._row_num"
    )


def _unparseable_rows_sql(table: str, column: str) -> str:
    keys = {"history": "application_id", "candidates": "candidate_id", "jobs": "job_id"}[table]
    return (
        f"select r.{col(keys)} as row_key, r.{col(column)} as raw_value "
        f"from raw.{table} r join stg.{table} s on r._row_num = s._row_num "
        f"where r.{col(column)} is not null and trim(r.{col(column)}) <> '' "
        f"and s.{col(column)} is null limit 25"
    )


def _missing_rows_sql(table: str, column: str) -> str:
    keys = {"history": "application_id", "candidates": "candidate_id", "jobs": "job_id"}[table]
    return (
        f"select {col(keys)} as row_key, '{column}' as missing_column from stg.{table} "
        f"where {_is_missing(column)} limit 25"
    )


def _missing_breakdown_sql(table: str, column: str) -> str:
    """Split the measured rate so a mismatch shows which slice of rows moved."""
    if table == "history":
        bucket = "strftime(try_cast(spontaneous_application_date as date), '%Y')"
    else:
        bucket = "strftime(try_cast(create_date as date), '%Y')"
    return (
        f"select coalesce({bucket}, 'undated') as year, count(*) as rows, "
        f"count(*) filter (where {_is_missing(column)}) as missing, "
        f"round(100.0 * count(*) filter (where {_is_missing(column)}) "
        f"/ count(*), 2) as pct_missing "
        f"from stg.{table} group by 1 order by 1"
    )


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<48} {c.severity:<8} {c.kind}")
