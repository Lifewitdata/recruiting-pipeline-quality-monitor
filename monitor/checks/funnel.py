"""Funnel logic: does the recruiting trajectory tell a consistent story?

JTH expresses the funnel as ten dated columns on one row rather than as an event
log, and adds a derived `last_stage_reached` label. That shape invites four
mistakes the checks here are aimed at: assuming the stages are strictly
sequential, assuming the first stage means "application", trusting the published
label to agree with the dates, and assuming a populated later date proves the
earlier ones.
"""

from __future__ import annotations

from monitor.card import STAGES, parse
from monitor.checks.base import Check, col

# The published label for each stage column, learned from the card's vocabulary and
# confirmed 1:1 against the dates by the conformance check below. Two names differ
# from their column: stage 0 is "Application Made", stage 8 is "Offer Received".
LABELS = [
    "Application Made",
    "Shortlist",
    "Qualification",
    "Resume Sent",
    "1st Interview",
    "2nd Interview",
    "3rd Interview",
    "4th Interview",
    "Offer Received",
    "Offer Accepted",
]

POP = {s: f"{col(s)} is not null" for s in STAGES}


def _deepest_rank() -> str:
    """Rank of the deepest populated stage.

    Conditions are built from the last stage backwards, because CASE returns the
    first match: written front-to-back this would silently measure the shallowest
    stage instead.
    """
    arms = " ".join(
        f"when {POP[STAGES[rank]]} then {rank}"
        for rank in range(len(STAGES) - 1, -1, -1)
    )
    return f"case {arms} else -1 end"


def _label_rank() -> str:
    return (
        "case last_stage_reached "
        + " ".join(f"when '{lab}' then {i}" for i, lab in enumerate(LABELS))
        + " else -99 end"
    )


def adjacent_pairs() -> list[tuple[str, str]]:
    return list(zip(STAGES[:-1], STAGES[1:]))


def all_pairs() -> list[tuple[str, str]]:
    """Every (earlier, later) stage combination: 45 pairs over ten stages."""
    return [
        (STAGES[i], STAGES[j])
        for i in range(len(STAGES))
        for j in range(i + 1, len(STAGES))
    ]


def build(claims: dict | None = None) -> list[Check]:
    claims = claims or parse()
    window = claims.get("window", {})
    checks: list[Check] = []

    # --- 1. no stage may be dated before an earlier one (all 45 pairs) -------
    inverted = " + ".join(
        f"count(*) filter (where {POP[a]} and {POP[b]} "
        f"and {col(b)} < {col(a)})"
        for a, b in all_pairs()
    )
    checks.append(
        Check(
            id="funnel.stage_order",
            family="funnel",
            scope="history.csv",
            name="no stage is dated before an earlier stage",
            severity="critical",
            kind="zero",
            unit="inverted pairs",
            description=(
                f"Checked all {len(all_pairs())} stage pairs, not just neighbours. The "
                "card states dates were jittered with 5-day Laplace noise then monotonic-sorted "
                "to preserve order, so an inversion here means that guarantee broke."
            ),
            sql=f"select ({inverted})::double from stg.history",
            expected=0.0,
            drilldown=" union all ".join(
                f"select '{a} -> {b}' as pair, count(*) filter (where {POP[a]} and {POP[b]} "
                f"and {col(b)} < {col(a)}) as inverted, "
                f"min({col(b)} - {col(a)}) as worst_days from stg.history"
                for a, b in all_pairs()
            ),
        )
    )

    # --- 2. an offer cannot be accepted before it is proposed ---------------
    checks.append(
        Check(
            id="funnel.accepted_without_proposed",
            family="funnel",
            scope="history.csv",
            name="no accepted offer lacks a proposed offer",
            severity="critical",
            kind="zero",
            unit="rows",
            description=(
                "The one stage transition that is genuinely impossible: you cannot accept an "
                "offer that was never made. Every other skip in this funnel is explainable by a "
                "recruiter leaving a field blank; this one makes the placement count wrong."
            ),
            sql=f"""
                select count(*) filter (where {POP[STAGES[9]]} and not {POP[STAGES[8]]})::double
                from stg.history
            """,
            expected=0.0,
            drilldown=f"""
                select application_id, candidate_id, job_id, last_stage_reached,
                       {col(STAGES[8])} as proposed, {col(STAGES[9])} as accepted,
                       end_of_process_date
                from stg.history
                where {POP[STAGES[9]]} and not {POP[STAGES[8]]}
                order by accepted
            """,
        )
    )

    # --- 3. the published label agrees with the dates -----------------------
    checks.append(
        Check(
            id="funnel.label_conformance",
            family="funnel",
            scope="history.csv",
            name="last_stage_reached equals the deepest dated stage",
            severity="critical",
            kind="zero",
            unit="disagreeing rows",
            description=(
                "The card describes last_stage_reached as the label of the deepest non-null "
                "stage, which makes it redundant with the dates. If the two ever disagree, "
                "dashboards grouped by the label and funnels computed from the dates will "
                "report different numbers on the same extract."
            ),
            sql=f"""
                select count(*)::double from (
                    select {_deepest_rank()} as date_rank,
                           {_label_rank()}  as label_rank
                    from stg.history
                ) where date_rank <> label_rank
            """,
            expected=0.0,
            drilldown=f"""
                select application_id, last_stage_reached as published_label,
                       {_deepest_rank()} as rank_from_dates, {_label_rank()} as rank_from_label
                from stg.history
                where {_deepest_rank()} <> {_label_rank()}
                limit 25
            """,
        )
    )

    # --- 4. label vocabulary -------------------------------------------------
    label_values = ", ".join(f"'{lab}'" for lab in LABELS)
    checks.append(
        Check(
            id="funnel.label_vocabulary",
            family="funnel",
            scope="history.csv",
            name="last_stage_reached stays inside the published vocabulary",
            severity="warning",
            kind="zero",
            unit="unknown labels",
            description=(
                "A new stage label is a schema change arriving through a data column. Groupbys "
                "silently gain a bucket and nobody notices which funnel step it replaced."
            ),
            sql=f"""
                select count(*) filter (where last_stage_reached not in ({label_values}))::double
                from stg.history
            """,
            expected=0.0,
            drilldown="""
                select last_stage_reached as label, count(*) as rows
                from stg.history
                where last_stage_reached not in
                    ('Application Made', 'Shortlist', 'Qualification', 'Resume Sent',
                     '1st Interview', '2nd Interview', '3rd Interview', '4th Interview',
                     'Offer Received', 'Offer Accepted')
                group by 1 order by rows desc
            """,
        )
    )

    # --- 5. skipped stages, one learned band per adjacent pair --------------
    # Pair 0 (spontaneous application -> shortlist) is excluded: see the
    # stage-one semantics check, which shows it is not a funnel step for most rows.
    for i in range(1, len(STAGES) - 1):
        shallow, deep = STAGES[i], STAGES[i + 1]
        checks.append(
            Check(
                id=f"funnel.skip.{shallow.replace('_date', '')}",
                family="funnel",
                scope="history.csv",
                name=f"{deep} populated while {shallow} is blank",
                severity="warning",
                kind="drift",
                unit="rows",
                tolerance=25.0,
                metric_key=f"skip.{shallow}",
                description=(
                    f"Trajectories that carry {deep} but no {shallow}. Recruiters leave "
                    "intermediate fields blank, so a non-zero value is expected and the "
                    "monitor bands it against previous runs rather than asserting zero. A jump "
                    "in the skip rate is what matters: it means a stage stopped being captured."
                ),
                sql=f"""
                    select count(*) filter (where {POP[deep]} and not {POP[shallow]})::double
                    from stg.history
                """,
                drilldown=f"""
                    select count(*) as skipped_rows,
                           count(*) filter (where end_of_process_date is null) as still_open,
                           min({col(deep)}) as earliest_deep_date,
                           max({col(deep)}) as latest_deep_date
                    from stg.history
                    where {POP[deep]} and not {POP[shallow]}
                """,
            )
        )

    # --- 6. stage one is not what its name says -----------------------------
    checks.append(
        Check(
            id="funnel.stage_one_semantics",
            family="funnel",
            scope="history.csv",
            name="share of shortlisted rows with no spontaneous application date",
            severity="warning",
            kind="drift",
            unit="% shortlisted",
            tolerance=2.0,
            metric_key="stage_one.spontaneous_absent",
            description=(
                "spontaneous_application_date sounds like step one of the funnel and is "
                "documented as a chronological funnel step. Measured, almost every shortlisted "
                "row has no value in it: it records unsolicited applications only. Any "
                "application-to-shortlist conversion rate computed off it is dividing by the "
                "wrong denominator."
            ),
            sql=f"""
                select round(100.0 * count(*) filter (where {POP[STAGES[1]]} and not {POP[STAGES[0]]})
                       / nullif(count(*) filter (where {POP[STAGES[1]]}), 0), 4)
                from stg.history
            """,
            drilldown=f"""
                select count(*) as rows_total,
                       count(*) filter (where {POP[STAGES[0]]}) as with_spontaneous_date,
                       count(*) filter (where {POP[STAGES[1]]}) as with_shortlist_date,
                       count(*) filter (where {POP[STAGES[0]]} and {POP[STAGES[1]]}) as both
                from stg.history
            """,
        )
    )

    # --- 7. end_of_process cannot precede the deepest stage -----------------
    greatest = "greatest(" + ", ".join(col(s) for s in STAGES) + ")"
    checks.append(
        Check(
            id="funnel.end_of_process_order",
            family="funnel",
            scope="history.csv",
            name="end_of_process_date is never before the deepest stage",
            severity="critical",
            kind="zero",
            unit="rows",
            description=(
                "The card defines end_of_process_date as the last recorded action, so it must "
                "be at or after every stage timestamp on the same row."
            ),
            sql=f"""
                select count(*) filter (
                    where end_of_process_date is not null
                      and end_of_process_date < {greatest})::double
                from stg.history
            """,
            expected=0.0,
            drilldown=f"""
                select application_id, last_stage_reached, end_of_process_date,
                       {greatest} as deepest_stage_date
                from stg.history
                where end_of_process_date is not null and end_of_process_date < {greatest}
                limit 25
            """,
        )
    )

    # --- 8. every date inside the published window --------------------------
    start, end = window.get("start", "2018-03-01"), window.get("end", "2025-04-30")
    all_dates = STAGES + ["end_of_process_date"]
    outside = " + ".join(
        f"count(*) filter (where {col(d)} is not null and ({col(d)} < date '{start}' "
        f"or {col(d)} > date '{end}'))"
        for d in all_dates
    )
    checks.append(
        Check(
            id="funnel.date_window",
            family="funnel",
            scope="history.csv",
            name=f"every funnel date falls inside {window.get('label', 'the published window')}",
            severity="warning",
            kind="zero",
            unit="out-of-window values",
            description=(
                f"The abstract publishes the collection window as {window.get('label', '')}. "
                "A timestamp outside it is either a leaked later extract or a corrupt date, and "
                "both change any time-series cut."
            ),
            sql=f"select ({outside})::double from stg.history",
            expected=0.0,
            drilldown=" union all ".join(
                f"select '{d}' as stage, count(*) filter (where {col(d)} is not null and "
                f"({col(d)} < date '{start}' or {col(d)} > date '{end}')) as outside, "
                f"max({col(d)}) as latest from stg.history"
                for d in all_dates
            ),
            reference="dataset_card.md - abstract window",
        )
    )

    # --- 9. stage timestamps after their own entity existed -----------------
    checks.append(
        Check(
            id="funnel.applied_before_entity_existed",
            family="funnel",
            scope="cross-table",
            name="no trajectory is dated before its candidate or job was created",
            severity="critical",
            kind="zero",
            unit="rows",
            description=(
                "Cross-table temporal consistency: a trajectory whose first dated stage "
                "precedes candidates.create_date or jobs.create_date is evidence that the "
                "jitter was applied per column instead of per entity."
            ),
            sql=f"""
                select (
                    count(*) filter (
                        where c.create_date is not null and h.{col(STAGES[0])} is not null
                          and h.{col(STAGES[0])} < c.create_date
                    ) + count(*) filter (
                        where j.create_date is not null and h.{col(STAGES[0])} is not null
                          and h.{col(STAGES[0])} < j.create_date
                    ))::double
                from stg.history h
                join stg.candidates c on c.candidate_id = h.candidate_id
                join stg.jobs j on j.job_id = h.job_id
            """,
            expected=0.0,
            drilldown=f"""
                select h.application_id, h.candidate_id, c.create_date as candidate_created,
                       h.job_id, j.create_date as job_created,
                       h.{col(STAGES[0])} as application_date
                from stg.history h
                join stg.candidates c on c.candidate_id = h.candidate_id
                join stg.jobs j on j.job_id = h.job_id
                where h.{col(STAGES[0])} is not null
                  and (h.{col(STAGES[0])} < c.create_date or h.{col(STAGES[0])} < j.create_date)
                limit 25
            """,
        )
    )

    # --- 10. stage latency, measured ---------------------------------------
    latency_pairs = [
        (STAGES[0], STAGES[1], "application to shortlist"),
        (STAGES[1], STAGES[2], "shortlist to qualification"),
        (STAGES[8], STAGES[9], "offer proposed to accepted"),
    ]
    for a, b, label in latency_pairs:
        checks.append(
            Check(
                id=f"funnel.latency.{label.replace(' ', '_')}",
                family="funnel",
                scope="history.csv",
                name=f"median days, {label}",
                severity="info",
                kind="drift",
                unit="days",
                tolerance=1.0,
                metric_key=f"latency.{a}",
                description=(
                    f"Median of {col(b)} - {col(a)} over rows carrying both. Reported as a "
                    "measured baseline: time-in-stage is the headline number of every "
                    "recruiting funnel report, and a half-day of drift in it is a story."
                ),
                sql=f"""
                    select round(quantile_cont({col(b)} - {col(a)}, 0.5)::double, 4)
                    from stg.history
                    where {POP[a]} and {POP[b]}
                """,
                drilldown=f"""
                    select count(*) as rows_both_dated,
                           round(avg({col(b)} - {col(a)}), 2) as mean_days,
                           quantile_cont({col(b)} - {col(a)}, 0.9) as p90_days,
                           count(*) filter (where {col(b)} = {col(a)}) as same_day
                    from stg.history
                    where {POP[a]} and {POP[b]}
                """,
            )
        )

    # --- 11. same-day transitions ------------------------------------------
    same_day = " + ".join(
        f"count(*) filter (where {POP[a]} and {POP[b]} and {col(b)} = {col(a)})"
        for a, b in adjacent_pairs()
    )
    both_dated = " + ".join(f"count(*) filter (where {POP[a]} and {POP[b]})" for a, b in adjacent_pairs())
    checks.append(
        Check(
            id="funnel.same_day_transitions",
            family="funnel",
            scope="history.csv",
            name="share of stage transitions recorded on the same day",
            severity="warning",
            kind="drift",
            unit="% transitions",
            tolerance=2.0,
            metric_key="same_day.share",
            description=(
                "Day-granularity timestamps plus 5-day jitter with monotonic sorting collapse "
                "many adjacent stages onto one date. A time-to-hire metric built on this "
                "funnel under-reports by construction, and this share is how much."
            ),
            sql=f"select round(100.0 * ({same_day}) / nullif(({both_dated}), 0), 4) from stg.history",
            drilldown=" union all ".join(
                f"select '{a} -> {b}' as pair, "
                f"count(*) filter (where {POP[a]} and {POP[b]}) as both_dated, "
                f"count(*) filter (where {POP[a]} and {POP[b]} and {col(b)} = {col(a)}) as same_day "
                f"from stg.history"
                for a, b in adjacent_pairs()
            ),
        )
    )
    return checks


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<48} {c.severity:<8} {c.kind}")
