"""Key structure: does every row join to exactly one thing?

These are the checks that a build would normally put in dbt. They live here as
monitor checks because on a third-party extract you cannot add a constraint to the
source - the only defence is to test the bytes on every arrival and report what the
grain actually is.
"""

from __future__ import annotations

from monitor.checks.base import Check, col

PK = {"candidates": "candidate_id", "jobs": "job_id", "history": "application_id"}
FK = [("candidate_id", "candidates"), ("job_id", "jobs")]


def build() -> list[Check]:
    checks: list[Check] = []

    for table, key in PK.items():
        checks.append(
            Check(
                id=f"structure.unique_key.{table}.{key}",
                family="structure",
                scope=f"{table}.csv",
                name=f"{key} identifies exactly one {table} row",
                severity="critical",
                kind="zero",
                unit="duplicate key groups",
                description=(
                    f"Counted {key} values appearing on more than one row. If this breaks, "
                    f"every join from {table}.csv fans out and silently multiplies the "
                    "measures hanging off it."
                ),
                sql=f"""
                    select count(*)::double from (
                        select {col(key)} from stg.{table}
                        group by 1 having count(*) > 1
                    )
                """,
                expected=0.0,
                drilldown=f"""
                    select {col(key)} as duplicated_key, count(*) as rows
                    from stg.{table}
                    group by 1 having count(*) > 1
                    order by rows desc limit 25
                """,
            )
        )
        checks.append(
            Check(
                id=f"structure.key_populated.{table}.{key}",
                family="structure",
                scope=f"{table}.csv",
                name=f"{key} is never null",
                severity="critical",
                kind="zero",
                unit="null keys",
                description="A null key cannot be joined, counted, or traced back to source.",
                sql=f"select count(*) filter (where {col(key)} is null)::double from stg.{table}",
                expected=0.0,
            )
        )

    for fk, parent in FK:
        parent_key = PK[parent]
        checks.append(
            Check(
                id=f"structure.fk_orphan.history.{fk}",
                family="structure",
                scope="history.csv",
                name=f"every history.{fk} resolves to a {parent}.csv row",
                severity="critical",
                kind="zero",
                unit="orphan rows",
                description=(
                    f"Trajectories pointing at a {parent} that is not in the extract. They "
                    "vanish from every inner-joined report, so the funnel shrinks without "
                    "an error anywhere."
                ),
                sql=f"""
                    select count(*)::double
                    from stg.history h
                    left join stg.{parent} p on h.{col(fk)} = p.{col(parent_key)}
                    where p.{col(parent_key)} is null
                """,
                expected=0.0,
                drilldown=f"""
                    select h.{col(fk)} as unresolved_key, count(*) as trajectory_rows
                    from stg.history h
                    left join stg.{parent} p on h.{col(fk)} = p.{col(parent_key)}
                    where p.{col(parent_key)} is null
                    group by 1 order by trajectory_rows desc limit 25
                """,
            )
        )
        checks.append(
            Check(
                id=f"structure.fk_null.history.{fk}",
                family="structure",
                scope="history.csv",
                name=f"history.{fk} is always populated",
                severity="critical",
                kind="zero",
                unit="null foreign keys",
                description="A null foreign key is an orphan that no join will ever surface.",
                sql=f"select count(*) filter (where {col(fk)} is null)::double from stg.history",
                expected=0.0,
            )
        )

    # --- coverage: entities that exist but never enter the funnel -----------
    for child, key in (("candidates", "candidate_id"), ("jobs", "job_id")):
        checks.append(
            Check(
                id=f"structure.coverage_untested.{child}",
                family="structure",
                scope=f"{child}.csv",
                name=f"share of {child}.csv rows that never reach history.csv",
                severity="warning",
                kind="drift",
                unit="% rows",
                tolerance=1.0,
                metric_key=f"coverage_untested.{child}",
                description=(
                    f"{child.capitalize()} rows with no trajectory at all. Cold inventory is "
                    "normal in a real ATS extract, so the monitor does not assert it is zero - "
                    "it learns what it usually is and reports when the share moves."
                ),
                sql=f"""
                    select round(100.0 * count(*) filter (
                        where not exists (
                            select 1 from stg.history h where h.{col(key)} = c.{col(key)}
                        )) / count(*), 4)
                    from stg.{child} c
                """,
                drilldown=f"""
                    select count(*) as untested_rows
                    from stg.{child} c
                    where not exists (select 1 from stg.history h where h.{col(key)} = c.{col(key)})
                """,
            )
        )

    # --- fan-out: what a naive per-entity metric would double count ---------
    checks.append(
        Check(
            id="structure.fanout.history_per_candidate",
            family="structure",
            scope="history.csv",
            name="trajectories per candidate",
            severity="info",
            kind="info",
            unit="max per candidate",
            description=(
                "The widest candidate in the extract. Averages hide this: any metric that "
                "assumes one row per candidate is measuring the most-applied person in the file."
            ),
            sql="""
                select max(n)::double from (
                    select candidate_id, count(*) n from stg.history group by 1
                )
            """,
            drilldown="""
                select candidate_id, count(*) as applications
                from stg.history group by 1 order by applications desc limit 10
            """,
        )
    )
    checks.append(
        Check(
            id="structure.fanout.history_per_job",
            family="structure",
            scope="history.csv",
            name="trajectories per job",
            severity="info",
            kind="info",
            unit="max per job",
            description=(
                "The most-applied vacancy in the extract, and the reason a job-level average "
                "must be weighted by applications rather than taken per trajectory."
            ),
            sql="""
                select max(n)::double from (
                    select job_id, count(*) n from stg.history group by 1
                )
            """,
            drilldown="""
                select job_id, count(*) as applications
                from stg.history group by 1 order by applications desc limit 10
            """,
        )
    )
    return checks


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<44} {c.severity:<8} {c.kind}")
