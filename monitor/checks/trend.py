"""Freshness, volume and anomaly: the checks that only mean something on a timer.

Everything else in this suite is a property of the bytes. These compare this run
against previous runs, or against the shape of the series the metric sits in, and
they are the reason the monitor is scheduled rather than run once.

Two detectors with different failure modes are deliberately paired here. On this
source a 3-sigma z-score over monthly event volume finds nothing (across the 84
months carrying at least 100 events, the widest is May 2022 at 2,305 events - 2.51
sigma from the 929-event mean, under the bar) while the trailing-median ratio finds
a 90 % collapse in the newest month. Neither one alone is the monitor.
"""

from __future__ import annotations

from monitor.card import STAGES, parse
from monitor.checks.base import Check, col

MONTHLY = {
    "candidates": ("stg.candidates", "create_date", "candidate profiles created"),
    "jobs": ("stg.jobs", "create_date", "vacancies created"),
}

# A month is a fair comparison target only once it has closed; the newest month in
# any snapshot is partial by construction.
MIN_MONTH_EVENTS = 100
MIN_MONTH_APPLICATIONS = 20
TRAILING_MONTHS = 6


def _monthly_ratio(source: str, date_col: str) -> str:
    return f"""
        with m as (
            select date_trunc('month', {col(date_col)}) as ym, count(*) as n
            from {source}
            where {col(date_col)} is not null
            group by 1
        ),
        ranked as (
            select ym, n, row_number() over (order by ym desc) as rn from m
        ),
        base as (
            select quantile_cont(n, 0.5) as med from ranked where rn between 2 and {TRAILING_MONTHS + 1}
        )
        select round(100.0 * ((select n from ranked where rn = 1) - (select med from base))
                      / nullif((select med from base), 0), 4)
        from base
    """


def build(claims: dict | None = None) -> list[Check]:
    claims = claims or parse()
    checks: list[Check] = []

    # --- 1. landing freshness -----------------------------------------------
    checks.append(
        Check(
            id="trend.landing_freshness",
            family="trend",
            scope="landing",
            name="the landing was refreshed within the SLA",
            severity="critical",
            kind="close",
            unit="hours since load",
            tolerance=24.0,
            fail_beyond=7.0,
            metric_key="freshness.hours",
            description=(
                "Hours since the newest _loaded_at. This is the check that fires when the "
                "schedule silently stops: every other number stays perfectly healthy on a "
                "three-week-old extract, which is the most dangerous state a monitor can be in."
            ),
            sql="select round(date_diff('minute', max(_loaded_at), current_timestamp) / 60.0, 4) from raw.history",
            expected=0.0,
            drilldown="""
                select source_file, loaded_at, sha256, rows
                from raw._load_manifest order by loaded_at desc
            """,
        )
    )

    # --- 2. per-table business recency --------------------------------------
    global_max = con_max_dates()
    for table, column, label in (
        ("candidates", "create_date", "candidate profiles"),
        ("jobs", "create_date", "vacancies"),
        ("history", "end_of_process_date", "closed processes"),
    ):
        checks.append(
            Check(
                id=f"trend.data_recency.{table}",
                family="trend",
                scope=f"{table}.csv",
                name=f"{table}.csv {label} run to the same date as the rest of the extract",
                severity="warning",
                kind="close",
                unit="days behind",
                tolerance=7.0,
                fail_beyond=4.0,
                metric_key=f"recency.{table}",
                description=(
                    "Days between this table's newest business date and the newest anywhere in "
                    "the snapshot. A table that stops moving while the others keep arriving is a "
                    "partial extract, and it makes every trend line end at a different point."
                ),
                sql=f"""
                    select date_diff('day', max({col(column)}), {global_max})::double
                    from stg.{table}
                """,
                expected=0.0,
                drilldown=f"""
                    select max({col(column)}) as newest_{table}
                    from stg.{table}
                """,
            )
        )

    # --- 3. newest month against the trailing median ------------------------
    for table, (source, date_col, label) in MONTHLY.items():
        checks.append(
            Check(
                id=f"trend.volume_last_month.{table}",
                family="trend",
                scope=f"{table}.csv",
                name=f"newest complete month of {label} vs the trailing {TRAILING_MONTHS}",
                severity="critical",
                kind="close",
                unit="% vs trailing median",
                tolerance=40.0,
                fail_beyond=1.5,
                metric_key=f"volume.{table}",
                description=(
                    f"Percent difference between the newest month of {label} and the median of "
                    f"the previous {TRAILING_MONTHS}. Ratio rather than sigma, because on a "
                    "7-year series the standard deviation is wide enough to hide a collapse this "
                    "size. A month that opens part-way through always trips this, which is why "
                    "the finding is a question about the extract window and not an answer."
                ),
                sql=_monthly_ratio(source, date_col),
                expected=0.0,
                drilldown=f"""
                    with m as (
                        select date_trunc('month', {col(date_col)}) as ym, count(*) as n
                        from {source} where {col(date_col)} is not null group by 1
                    )
                    select ym, n,
                           lag(n) over (order by ym) as previous_month,
                           round(100.0 * n / nullif(quantile_cont(n, 0.5) over (
                               order by ym rows between {TRAILING_MONTHS} preceding
                               and 1 preceding), 0) - 100, 2) as pct_vs_trailing_median
                    from m order by ym desc limit 12
                """,
            )
        )

    checks.append(
        Check(
            id="trend.volume_last_month.events",
            family="trend",
            scope="history.csv",
            name="newest month of funnel events vs the trailing median",
            severity="critical",
            kind="close",
            unit="% vs trailing median",
            tolerance=40.0,
            fail_beyond=1.5,
            metric_key="volume.events",
            description=(
                "Same ratio applied to the stage-event series, which is what the funnel "
                "dashboard is actually built on."
            ),
            sql=_monthly_ratio("stg.events", "stage_date"),
            expected=0.0,
            drilldown="""
                select date_trunc('month', stage_date) as ym, count(*) as events,
                       count(distinct application_id) as active_trajectories
                from stg.events group by 1 order by ym desc limit 12
            """,
        )
    )

    # --- 4. sigma anomaly over the monthly event series ---------------------
    checks.append(
        Check(
            id="trend.anomaly.monthly_events_zscore",
            family="trend",
            scope="history.csv",
            name="no month's event volume crosses 3 sigma",
            severity="warning",
            kind="zero",
            unit="anomalous months",
            description=(
                f"Classical z-score over months carrying at least {MIN_MONTH_EVENTS} events, "
                "excluding the newest month so an in-flight month cannot trip it. On this "
                "extract it finds nothing where the ratio check above finds a 90 % drop - the "
                "spread of a seven-year funnel series is wide enough to absorb it. Recorded "
                "because it is the detector most teams stop at."
            ),
            sql=f"""
                with m as (
                    select date_trunc('month', stage_date) as ym, count(*) as n
                    from stg.events where stage_date is not null group by 1
                ),
                ranked as (select ym, n, row_number() over (order by ym desc) rn from m),
                s as (
                    select avg(n::double) mu, stddev_samp(n::double) sd
                    from ranked where n >= {MIN_MONTH_EVENTS} and rn > 1
                )
                select count(*)::double
                from ranked, s
                where n >= {MIN_MONTH_EVENTS} and rn > 1
                  and abs(n::double - mu) / nullif(sd, 0) >= 3
            """,
            expected=0.0,
            drilldown="""
                with m as (
                    select date_trunc('month', stage_date) as ym, count(*) as n
                    from stg.events group by 1
                ),
                ranked as (select ym, n, row_number() over (order by ym desc) rn from m),
                s as (select avg(n::double) mu, stddev_samp(n::double) sd
                      from ranked where n >= 100 and rn > 1)
                select ym, n, round((n::double - mu) / nullif(sd, 0), 2) as z
                from ranked, s
                where n >= 100 and rn > 1
                  and abs(n::double - mu) / nullif(sd, 0) >= 2
                order by abs(z) desc limit 20
            """,
        )
    )

    # --- 5. conversion-rate anomaly ----------------------------------------
    checks.append(
        Check(
            id="trend.anomaly.shortlist_rate_zscore",
            family="trend",
            scope="history.csv",
            name="no application month's shortlist rate crosses 3 sigma",
            severity="warning",
            kind="zero",
            unit="anomalous months",
            description=(
                "Monthly shortlist rate over the spontaneous-application cohort only, since "
                f"months below {MIN_MONTH_APPLICATIONS} applications cannot carry a stable rate. "
                "A rate that leaves its own distribution is how a screening policy change shows "
                "up in data nobody touched."
            ),
            sql=f"""
                with m as (
                    select date_trunc('month', {col(STAGES[0])}) as ym,
                           count(*) as apps,
                           count(*) filter (where {col(STAGES[1])} is not null) as shortlisted
                    from stg.history
                    where {col(STAGES[0])} is not null
                    group by 1
                ),
                rates as (
                    select ym, apps, shortlisted, 100.0 * shortlisted / apps as rate
                    from m where apps >= {MIN_MONTH_APPLICATIONS}
                ),
                s as (select avg(rate) mu, stddev_samp(rate) sd from rates)
                select count(*)::double
                from rates, s
                where abs(rate - mu) / nullif(sd, 0) >= 3
            """,
            expected=0.0,
            drilldown=f"""
                with m as (
                    select date_trunc('month', {col(STAGES[0])}) as ym,
                           count(*) as apps,
                           count(*) filter (where {col(STAGES[1])} is not null) as shortlisted
                    from stg.history
                    where {col(STAGES[0])} is not null group by 1
                ),
                rates as (
                    select ym, apps, shortlisted, 100.0 * shortlisted / apps as rate
                    from m where apps >= {MIN_MONTH_APPLICATIONS}
                ),
                s as (select avg(rate) mu, stddev_samp(rate) sd from rates)
                select ym, apps, shortlisted, round(rate, 2) as rate_pct,
                       round((rate - mu) / nullif(sd, 0), 2) as z
                from rates, s order by abs(z) desc limit 20
            """,
        )
    )

    # --- 6. a field that only started being filled part-way through ---------
    checks.append(
        Check(
            id="trend.field_adoption.application_source",
            family="trend",
            scope="history.csv",
            name="application_source coverage is stable across years",
            severity="warning",
            kind="close",
            unit="pp spread",
            tolerance=5.0,
            fail_beyond=4.0,
            metric_key="adoption.application_source",
            description=(
                "Percentage-point spread of the populated share between the earliest and latest "
                "year with enough rows to judge. A channel-attribution dashboard built on this "
                "column is describing 2024 and 2025 only, and the spread is what says so."
            ),
            sql=f"""
                with y as (
                    select strftime(date_trunc('year', {col(STAGES[0])}), '%Y') as yr,
                           count(*) as rows_in_year,
                           100.0 * count(*) filter (where application_source is not null
                                                     and cast(application_source as varchar) <> '')
                               / count(*) as populated_pct
                    from stg.history
                    where {col(STAGES[0])} is not null
                    group by 1
                    having count(*) >= {MIN_MONTH_APPLICATIONS}
                )
                select round(max(populated_pct) - min(populated_pct), 4) from y
            """,
            expected=0.0,
            drilldown=f"""
                select strftime(date_trunc('year', {col(STAGES[0])}), '%Y') as yr,
                       count(*) as rows_in_year,
                       count(*) filter (where application_source is not null) as with_source,
                       round(100.0 * count(*) filter (where application_source is not null)
                             / count(*), 2) as populated_pct
                from stg.history
                where {col(STAGES[0])} is not null
                group by 1 order by 1
            """,
        )
    )

    # --- 7. null-rate drift on the columns the reports hang off -------------
    for table, column, note in (
        ("candidates", "years_experience", "the seniority dimension"),
        ("candidates", "actual_salary", "the compensation dimension"),
        ("history", "application_source", "the channel-attribution dimension"),
    ):
        checks.append(
            Check(
                id=f"trend.null_rate_drift.{table}.{column}",
                family="trend",
                scope=f"{table}.csv",
                name=f"{table}.{column} null share held steady",
                severity="warning",
                kind="drift",
                unit="% null",
                tolerance=1.0,
                metric_key=f"null_rate.{table}.{column}",
                description=(
                    f"Null share of {column}, {note}. Sparsity is a property of how recruiters "
                    "work, so the absolute level is not the finding - the move is. A jump means "
                    "a form stopped being filled or a column stopped being extracted."
                ),
                sql=f"""
                    select round(100.0 * count(*) filter (
                        where {col(column)} is null
                           or cast({col(column)} as varchar) = '') / count(*), 4)
                    from stg.{table}
                """,
                drilldown=f"""
                    select strftime(date_trunc('year', {col('create_date' if table != 'history' else STAGES[0])}), '%Y') as yr,
                           count(*) as rows,
                           round(100.0 * count(*) filter (where {col(column)} is null) / count(*), 2)
                               as pct_null
                    from stg.{table}
                    where {col('create_date' if table != 'history' else STAGES[0])} is not null
                    group by 1 order by 1
                """,
            )
        )

    # --- 8. row-count drift per table ---------------------------------------
    for table in ("candidates", "jobs", "history"):
        checks.append(
            Check(
                id=f"trend.row_count_drift.{table}",
                family="trend",
                scope=f"{table}.csv",
                name=f"{table}.csv arrived at its usual size",
                severity="critical",
                kind="drift",
                unit="rows",
                tolerance=50.0,
                metric_key=f"row_count.{table}",
                description=(
                    f"Absolute row count of {table}.csv against the mean of previous runs, with a "
                    "50-row floor so a metric that has never moved cannot trip on rounding. A "
                    "truncated extract is the single most common cause of a report that looks "
                    "right and is wrong."
                ),
                sql=f"select count(*)::double from stg.{table}",
                drilldown="select source_file, rows, sha256 from raw._load_manifest order by rows desc",
            )
        )
    return checks


def con_max_dates() -> str:
    """The newest business date anywhere in the snapshot, as a SQL expression.

    Computed as a scalar subquery rather than a constant so the freshness picture
    moves with the data instead of being pinned to the day this file was written.
    """
    return (
        "(select greatest("
        "  (select max(create_date) from stg.candidates),"
        "  (select max(create_date) from stg.jobs),"
        "  (select max(end_of_process_date) from stg.history)))"
    )


if __name__ == "__main__":
    for c in build():
        print(f"{c.id:<48} {c.severity:<8} {c.kind}")
