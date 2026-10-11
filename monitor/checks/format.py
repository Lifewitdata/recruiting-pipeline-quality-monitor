"""Value formats and domains.

Three defect classes that survive every schema check and still break a report:
a column typed as text that arrived float-serialised, a code whose leading zero
was destroyed on the way through a dataframe, and a documented value domain that
the file does not actually use.
"""

from __future__ import annotations

import re

from monitor.card import parse
from monitor.checks.base import Check, col

# A value like '75.0' in a column the card types as `str`: the signature of an
# integer column that passed through a float dtype on the way out of the source.
FLOAT_SERIALISED = r"^[0-9]+\.0$"
CANONICAL_DEPARTEMENT = r"^[0-9]{2}$"

# Zipcode columns: the card promises a 2-digit departement code in both tables.
ZIPCODE_COLUMNS = ("candidates", "jobs")


def _str_columns(claims: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for table, types in claims.get("types", {}).items():
        out[table] = [
            c for c, declared in types.items()
            if declared.strip().startswith("`str`") and not c.startswith("llm_")
        ]
    return out


def _binned_domain(note: str) -> list[str]:
    """Tokens the card lists after `binned:` - e.g. `0-2`, `3-6`, `+6 years`."""
    if "binned" not in note:
        return []
    tail = note.split("binned", 1)[1]
    return re.findall(r"`([^`]+)`", tail)


def build(claims: dict | None = None) -> list[Check]:
    claims = claims or parse()
    checks: list[Check] = []

    # --- 1. text columns that arrived float-serialised ----------------------
    for table, columns in _str_columns(claims).items():
        for column in columns:
            checks.append(
                Check(
                    id=f"format.text_type_fidelity.{table}.{column}",
                    family="format",
                    scope=f"{table}.csv",
                    name=f"{table}.{column} holds text, not float-serialised numbers",
                    severity="warning",
                    kind="zero",
                    unit="values",
                    description=(
                        f"The card types {table}.{column} as str. Counted values shaped like a "
                        "float with a trailing .0, which is what an integer column looks like "
                        "after it has been through a float dtype. Every distinct-value count, "
                        "join and group-by changes shape when this happens."
                    ),
                    sql=f"""
                        select count(*) filter (
                            where {col(column)} is not null
                              and regexp_matches(cast({col(column)} as varchar),
                                                 '{FLOAT_SERIALISED}'))::double
                        from stg.{table}
                    """,
                    expected=0.0,
                    drilldown=f"""
                        select {col(column)} as raw_value, count(*) as rows
                        from stg.{table}
                        where {col(column)} is not null
                          and regexp_matches(cast({col(column)} as varchar), '{FLOAT_SERIALISED}')
                        group by 1 order by rows desc limit 25
                    """,
                    reference="dataset_card.md - declared column type",
                )
            )

    # --- 2. departement codes that need zero padding ------------------------
    for table in ZIPCODE_COLUMNS:
        checks.append(
            Check(
                id=f"format.zipcode_canonical.{table}",
                family="format",
                scope=f"{table}.csv",
                name=f"{table}.zipcode is a 2-digit departement code",
                severity="warning",
                kind="zero",
                unit="values",
                description=(
                    f"The card describes {table}.zipcode as a French departement code, 2-digit "
                    "and coarsened. Counted populated values that are not exactly two digits. A "
                    "departement code is a string, not a number: padding decides whether 05 and "
                    "5 group together."
                ),
                sql=f"""
                    select count(*) filter (
                        where {col("zipcode")} is not null
                          and not regexp_matches(cast({col("zipcode")} as varchar),
                                                 '{CANONICAL_DEPARTEMENT}'))::double
                    from stg.{table}
                """,
                expected=0.0,
                drilldown=f"""
                    select {col("zipcode")} as raw_value, count(*) as rows,
                           length(cast({col("zipcode")} as varchar)) as chars
                    from stg.{table}
                    where {col("zipcode")} is not null
                      and not regexp_matches(cast({col("zipcode")} as varchar),
                                             '{CANONICAL_DEPARTEMENT}')
                    group by 1, 3 order by rows desc limit 25
                """,
                reference="dataset_card.md - zipcode note",
            )
        )
        checks.append(
            Check(
                id=f"format.zipcode_leading_zero_lost.{table}",
                family="format",
                scope=f"{table}.csv",
                name=f"{table}.zipcode keeps the leading zero for departements 01-09",
                severity="warning",
                kind="zero",
                unit="values",
                description=(
                    "Codes 01-19 begin with a zero. Anything below 10 that reaches the file "
                    "without it cannot be told apart from a truncated code, and any map or "
                    "regional group-by splits the departement in two."
                ),
                sql=f"""
                    select count(*) filter (
                        where {col("zipcode")} is not null
                          and try_cast(cast({col("zipcode")} as varchar) as double) < 10
                          and not regexp_matches(cast({col("zipcode")} as varchar), '^0[0-9]$')
                    )::double
                    from stg.{table}
                """,
                expected=0.0,
                drilldown=f"""
                    select {col("zipcode")} as raw_value, count(*) as rows
                    from stg.{table}
                    where {col("zipcode")} is not null
                      and try_cast(cast({col("zipcode")} as varchar) as double) < 10
                      and not regexp_matches(cast({col("zipcode")} as varchar), '^0[0-9]$')
                    group by 1 order by rows desc
                """,
            )
        )
        checks.append(
            Check(
                id=f"format.zipcode_valid_departement.{table}",
                family="format",
                scope=f"{table}.csv",
                name=f"{table}.zipcode names a real departement",
                severity="warning",
                kind="zero",
                unit="values",
                description=(
                    "Metropolitan departement codes run 01-95 (2A/2B are Corsica). Zero is not a "
                    "departement, and neither is anything above 95, so these rows cannot be "
                    "placed on any map or rolled into any region."
                ),
                sql=f"""
                    select count(*) filter (
                        where {col("zipcode")} is not null
                          and try_cast(cast({col("zipcode")} as varchar) as double) is not null
                          and (try_cast(cast({col("zipcode")} as varchar) as double) < 1
                               or try_cast(cast({col("zipcode")} as varchar) as double) > 95)
                    )::double
                    from stg.{table}
                """,
                expected=0.0,
                drilldown=f"""
                    select {col("zipcode")} as raw_value, count(*) as rows
                    from stg.{table}
                    where {col("zipcode")} is not null
                      and try_cast(cast({col("zipcode")} as varchar) as double) is not null
                      and (try_cast(cast({col("zipcode")} as varchar) as double) < 1
                           or try_cast(cast({col("zipcode")} as varchar) as double) > 95)
                    group by 1 order by rows desc
                """,
            )
        )

    # --- 3. whitespace standing in for a missing value ---------------------
    for table in ZIPCODE_COLUMNS:
        checks.append(
            Check(
                id=f"format.blank_string_missing.{table}.zipcode",
                family="format",
                scope=f"{table}.csv",
                name=f"{table}.zipcode uses NULL, not whitespace, for missing",
                severity="warning",
                kind="zero",
                unit="values",
                description=(
                    "A cell of spaces is not null and is not empty, so it counts as populated in "
                    "every coverage figure and lands in its own bucket in every group-by."
                ),
                sql=f"""
                    select count(*) filter (
                        where {col("zipcode")} is not null
                          and cast({col("zipcode")} as varchar) <> ''
                          and trim(cast({col("zipcode")} as varchar)) = '')::double
                    from stg.{table}
                """,
                expected=0.0,
                reference="dataset_card.md - zipcode note",
            )
        )

    # --- 4. documented value domains ---------------------------------------
    for table, notes in claims.get("notes", {}).items():
        for column, note in notes.items():
            if column.startswith("llm_"):
                continue
            tokens = _binned_domain(note)
            if not tokens:
                continue
            quoted = ", ".join(f"'{t}'" for t in tokens)
            checks.append(
                Check(
                    id=f"format.domain.{table}.{column}",
                    family="format",
                    scope=f"{table}.csv",
                    name=f"{table}.{column} stays inside the documented bins",
                    severity="warning",
                    kind="zero",
                    unit="rows",
                    description=(
                        f"The card documents {column} as binned to {tokens}. Values outside a "
                        "published domain are what a bucketed chart quietly drops."
                    ),
                    sql=f"""
                        select count(*) filter (
                            where {col(column)} is not null
                              and cast({col(column)} as varchar) <> ''
                              and {col(column)} not in ({quoted}))::double
                        from stg.{table}
                    """,
                    expected=0.0,
                    drilldown=f"""
                        select {col(column)} as value, count(*) as rows
                        from stg.{table}
                        where {col(column)} is not null
                          and cast({col(column)} as varchar) <> ''
                          and {col(column)} not in ({quoted})
                        group by 1 order by rows desc
                    """,
                    reference="dataset_card.md - binned domain",
                )
            )

    # --- 5. semicolon lists: domain after the split ------------------------
    for table in ("candidates", "jobs"):
        checks.append(
            Check(
                id=f"format.list_domain.{table}.contract_type",
                family="format",
                scope=f"{table}.csv",
                name=f"{table}.contract_type tokens stay in the published vocabulary",
                severity="warning",
                kind="zero",
                unit="unknown tokens",
                description=(
                    "contract_type is a semicolon list, so an accepted-values test on the raw "
                    "column tests the wrong thing. The list is split first and each token is "
                    "checked against the union of both tables' vocabulary."
                ),
                sql=f"""
                    select count(*)::double
                    from stg.{table},
                         unnest(string_split(cast({col("contract_type")} as varchar), ';'))
                              as t(token)
                    where {col("contract_type")} is not null
                      and trim(token) not in ('permanent', 'freelance', 'fixed-term', 'internship')
                """,
                expected=0.0,
                drilldown=f"""
                    select trim(token) as token, count(*) as rows
                    from stg.{table},
                         unnest(string_split(cast({col("contract_type")} as varchar), ';'))
                              as t(token)
                    where {col("contract_type")} is not null
                      and trim(token) not in ('permanent', 'freelance', 'fixed-term', 'internship')
                    group by 1 order by rows desc
                """,
            )
        )

    # --- 6. the list/scalar split between the two tables -------------------
    checks.append(
        Check(
            id="format.list_shape.contract_type",
            family="format",
            scope="cross-table",
            name="contract_type is a list in candidates and a scalar in jobs",
            severity="info",
            kind="info",
            unit="multi-value rows",
            description=(
                "Rows where candidates.contract_type carries more than one token. The same field "
                "in jobs.csv never does, so any query that joins on contract_type equality is "
                "comparing a list to a scalar."
            ),
            sql="""
                select count(*) filter (
                    where "contract_type" is not null
                      and contains(cast("contract_type" as varchar), ';'))::double
                from stg.candidates
            """,
            drilldown="""
                select 'candidates' as source, count(*) as rows_with_list
                from stg.candidates
                where "contract_type" is not null and contains(cast("contract_type" as varchar), ';')
                union all
                select 'jobs', count(*)
                from stg.jobs
                where "contract_type" is not null and contains(cast("contract_type" as varchar), ';')
            """,
        )
    )

    # --- 7. k-anonymity sentinels, measured per table ---------------------
    for table in ("candidates", "jobs"):
        checks.append(
            Check(
                id=f"format.sentinel.{table}._rare_skill_",
                family="format",
                scope=f"{table}.csv",
                name=f"share of {table} rows carrying the _rare_skill_ sentinel",
                severity="info",
                kind="drift",
                unit="% rows",
                tolerance=1.0,
                metric_key=f"sentinel.{table}",
                description=(
                    "_rare_skill_ is the card's declared stand-in for an infrequent skill, so it "
                    "is suppressed for anonymity. It is counted here because it lands in the top "
                    "of any skill frequency table and is not a skill."
                ),
                sql=f"""
                    select round(100.0 * count(*) filter (
                        where {col("skills")} is not null
                          and contains(cast({col("skills")} as varchar), '_rare_skill_'))
                        / count(*), 4)
                    from stg.{table}
                """,
                drilldown=f"""
                    select count(*) as rows_total,
                           count(*) filter (where {col("skills")} is null) as skills_null,
                           count(*) filter (where contains(cast({col("skills")} as varchar),
                                                           '_rare_skill_')) as sentinel_rows
                    from stg.{table}
                """,
                reference="dataset_card.md - skills note",
            )
        )

    # --- 8. the salary clip the card says was applied ----------------------
    for table, column in (("candidates", "actual_salary"), ("jobs", "salary")):
        checks.append(
            Check(
                id=f"format.salary_clipped.{table}",
                family="format",
                scope=f"{table}.csv",
                name=f"no negative salary survived in {table}.{column}",
                severity="critical",
                kind="zero",
                unit="rows",
                description=(
                    f"The card states negative salaries created by the Laplace noise were clipped "
                    f"to zero. Verified against {column} directly - a negative salary is a value "
                    "no recruiter entered and no report should ever display."
                ),
                sql=f"""
                    select count(*) filter (where {col(column)} < 0)::double from stg.{table}
                """,
                expected=0.0,
                drilldown=f"""
                    select count(*) filter (where {col(column)} < 0) as negatives,
                           count(*) filter (where {col(column)} = 0) as zeros_clipped_to,
                           min({col(column)}) as minimum, max({col(column)}) as maximum,
                           count({col(column)}) as populated
                    from stg.{table}
                """,
                reference="dataset_card.md - ethical and privacy notes",
            )
        )
    return checks


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<52} {c.severity:<8} {c.kind}")
