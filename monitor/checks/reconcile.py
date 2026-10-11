"""Metric-vs-source reconciliation: compute the same number two ways and make it tie.

A warehouse test proves a model ran. These prove the number a dashboard shows is
still the number the raw data says, by routing every headline metric through a
second, independent path - the wide stage columns against the unpivoted event log -
and failing when the two disagree.

It also records where one metric name has two defensible denominators, because on a
funnel this sparse that gap is larger than most real changes in the business.
"""

from __future__ import annotations

from monitor.card import STAGES, parse
from monitor.checks.base import Check, col
from monitor.checks.funnel import LABELS, POP



def build(claims: dict | None = None) -> list[Check]:
    claims = claims or parse()
    checks: list[Check] = []

    # --- 1. every stage count, wide path vs event path ----------------------
    for stage, label in zip(STAGES, LABELS):
        checks.append(
            Check(
                id=f"reconcile.stage_population.{stage.removesuffix('_date')}",
                family="reconcile",
                scope="history.csv",
                name=f"{label} population ties between the wide table and the event log",
                severity="critical",
                kind="equal",
                unit="trajectories",
                description=(
                    f"One count from the {stage} column on the wide row, one from the unpivoted "
                    f"event log filtered to stage '{stage}'. Two code paths, one number: if an "
                    "extract starts half-filling the column, or the unpivot loses a stage, the "
                    "two stop agreeing and this is where it surfaces."
                ),
                sql=f"""
                    select (select count(*) filter (where {POP[stage]})
                            from stg.history)::double as wide_count,
                           (select count(*) from stg.events where stage = '{stage}')::double
                """,
                drilldown=f"""
                    select 'wide' as path, count(*) filter (where {POP[stage]}) as trajectories
                    from stg.history
                    union all
                    select 'event log', count(*) from stg.events where stage = '{stage}'
                """,
            )
        )

    # --- 3. the placements KPI, label path vs date path ---------------------
    checks.append(
        Check(
            id="reconcile.metric_placements",
            family="reconcile",
            scope="history.csv",
            name="placement count ties whether read from the label or the dates",
            severity="critical",
            kind="equal",
            unit="placements",
            description=(
                "Placements grouped by the published last_stage_reached label against placements "
                "counted from job_offer_accepted_date in the event log. They agree today only "
                "because the label happens to track the dates exactly; that is the assumption "
                "this check holds."
            ),
            sql=f"""
                select (select count(*) filter (where last_stage_reached = 'Offer Accepted')
                        from stg.history)::double,
                       (select count(distinct application_id)
                        from stg.events where stage = '{STAGES[9]}')::double
            """,
            drilldown=f"""
                select 'by label' as path, count(*) as placements
                from stg.history where last_stage_reached = 'Offer Accepted'
                union all
                select 'by date', count(distinct application_id)
                from stg.events where stage = '{STAGES[9]}'
            """,
        )
    )

    # --- 4. the event total the whole funnel rests on -----------------------
    stage_sum = " + ".join(f"count(*) filter (where {POP[s]})" for s in STAGES)
    checks.append(
        Check(
            id="reconcile.event_log_completeness",
            family="reconcile",
            scope="history.csv",
            name="event log holds every populated stage cell in the wide table",
            severity="critical",
            kind="equal",
            unit="events",
            description=(
                "Row count of the unpivoted event log against the sum of the ten stage columns. "
                "The unpivot is the only place a stage can be dropped without a query failing, "
                "so the totals are compared on every run."
            ),
            sql=f"""
                select (select count(*) from stg.events)::double,
                       ({stage_sum})::double
                from stg.history
            """,
            drilldown=" union all ".join(
                f"select '{s}' as stage, count(*) filter (where {POP[s]}) as wide, "
                f"(select count(*) from stg.events where stage = '{s}') as logged from stg.history"
                for s in STAGES
            ),
        )
    )

    # --- 5. the mandatory chain cannot get wider as it goes deeper ----------
    chain = [STAGES[1], STAGES[2], STAGES[3], STAGES[4]]
    counts = {s: f"count(*) filter (where {POP[s]})" for s in chain}
    named = ", ".join(f"{counts[s]} as \"{s}\"" for s in chain)
    steps = " + ".join(
        f'case when "{a}" < "{b}" then 1 else 0 end'
        for a, b in zip(chain[:-1], chain[1:])
    )
    checks.append(
        Check(
            id="reconcile.funnel_chain_monotone",
            family="reconcile",
            scope="history.csv",
            name="mandatory chain narrows at every step",
            severity="critical",
            kind="zero",
            unit="inverted steps",
            description=(
                "Shortlist, qualification, resume sent, first interview: each must reach fewer "
                "trajectories than the one above it. A deeper stage carrying more rows than its "
                "predecessor is not a business result, it is a partial load."
            ),
            sql=f"with stage_counts as (select {named} from stg.history) "
                f"select ({steps})::double from stage_counts",
            expected=0.0,
            drilldown=" union all ".join(
                f"select '{s}' as stage, count(*) filter (where {POP[s]}) as trajectories "
                f"from stg.history"
                for s in chain
            ),
        )
    )

    # --- 6. the label distribution is a partition, not a guess --------------
    checks.append(
        Check(
            id="reconcile.label_partition",
            family="reconcile",
            scope="history.csv",
            name="last_stage_reached buckets partition the trajectories exactly",
            severity="critical",
            kind="equal",
            unit="trajectories",
            description=(
                "Sum of the ten stage buckets against the row count. A funnel chart built from "
                "the label is only honest if the buckets add up to everything and nothing twice."
            ),
            sql=f"""
                select (select sum(n)::double from (
                          select count(*) n from stg.history
                          where last_stage_reached in ({", ".join(f"'{l}'" for l in LABELS)})
                          group by last_stage_reached
                       )) as bucketed,
                       (select count(*)::double from stg.history)
            """,
            drilldown="""
                select last_stage_reached as label, count(*) as trajectories
                from stg.history group by 1 order by trajectories desc
            """,
        )
    )

    # --- 7. one metric name, two defensible denominators -------------------
    checks.append(
        Check(
            id="reconcile.placement_rate_denominator_gap",
            family="reconcile",
            scope="history.csv",
            name="spread between the two defensible offer-accept rates",
            severity="info",
            kind="info",
            unit="pp apart",
            description=(
                "Offer-accept rate over every trajectory against the same count over offers "
                "actually proposed. Both are defensible readings of the same column pair and "
                "they are far apart, which is why the definition is published next to the "
                "number rather than assumed."
            ),
            sql=f"""
                select round(
                    100.0 * count(*) filter (where {POP[STAGES[9]]})
                        / nullif(count(*) filter (where {POP[STAGES[8]]}), 0)
                  - 100.0 * count(*) filter (where {POP[STAGES[9]]}) / count(*), 4)
                from stg.history
            """,
            drilldown=f"""
                select count(*) as trajectories,
                       count(*) filter (where {POP[STAGES[8]]}) as offers_proposed,
                       count(*) filter (where {POP[STAGES[9]]}) as offers_accepted,
                       round(100.0 * count(*) filter (where {POP[STAGES[9]]}) / count(*), 2)
                           as pct_of_all_trajectories,
                       round(100.0 * count(*) filter (where {POP[STAGES[9]]})
                             / nullif(count(*) filter (where {POP[STAGES[8]]}), 0), 2)
                           as pct_of_proposed
                from stg.history
            """,
        )
    )
    return checks


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<52} {c.severity:<8} {c.kind}")
