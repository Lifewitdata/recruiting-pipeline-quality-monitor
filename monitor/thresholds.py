"""Thresholds learned from the monitor's own run history.

A hardcoded `alert if null rate > 5 %` is a guess somebody typed once and never
revisited. Here a threshold is derived from what the metric has actually done: the
mean and standard deviation of previous runs, with an absolute floor so a metric
that has never moved cannot produce a zero-width band and page on the third
decimal place.

Until a metric has MIN_HISTORY runs there is no distribution to learn from, so the
check reports that it is seeding rather than pretending to have a baseline. That
state is visible in the result detail, because "we do not know normal yet" is a
different answer from "normal is 5 %" and should not be faked.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_HISTORY = 3
SIGMA = 3.0


@dataclass
class Baseline:
    metric_key: str
    mean: float
    sd: float
    n_runs: int
    min_tol: float

    @property
    def usable(self) -> bool:
        return self.n_runs >= MIN_HISTORY

    @property
    def tolerance(self) -> float:
        return max(SIGMA * self.sd, self.min_tol)


class LearnedThresholds:
    def __init__(self, con) -> None:
        self.con = con

    def record(self, run_id: str, metric_key: str, value: float, min_tol: float = 0.0) -> None:
        self.con.execute(
            "insert into dq.metrics (run_id, recorded_at, metric_key, bucket, value) "
            "values (?, current_timestamp, ?, 'run', ?)",
            [run_id, metric_key, value],
        )
        # The floor is a property of the check, not of the history, so it is
        # carried on the baseline row for the dashboard to show alongside the band.
        self.con.execute(
            "insert or ignore into dq.baselines "
            "(metric_key, mean, sd, n_runs, last_value, min_tol, updated_at) "
            "values (?, ?, ?, ?, ?, ?, current_timestamp)",
            [metric_key, value, 0.0, 1, value, min_tol],
        )
        self.con.execute(
            "update dq.baselines set min_tol = ? where metric_key = ?", [min_tol, metric_key]
        )

    def baseline(self, metric_key: str, min_tol: float) -> Baseline | None:
        row = self.con.execute(
            """
            select count(*) n_runs, coalesce(mean(value), 0.0) m, coalesce(stddev_samp(value), 0.0) sd
            from dq.metrics
            where metric_key = ? and bucket = 'run'
            """,
            [metric_key],
        ).fetchone()
        n, mean, sd = int(row[0]), float(row[1]), float(row[2])
        if n == 0:
            return None
        return Baseline(metric_key, mean, sd, n, min_tol)

    def refresh(self) -> int:
        """Materialise the learned stats so the dashboard can show the basis.

        An UPDATE rather than a rebuild: min_tol belongs to the check definition
        and must survive the statistics being recomputed.
        """
        self.con.execute(
            """
            create or replace temporary table _learned as
            select metric_key,
                   mean(value)                     as mean,
                   coalesce(stddev_samp(value), 0.0) as sd,
                   count(*)                        as n_runs,
                   max_by(value, recorded_at)      as last_value
            from dq.metrics
            where bucket = 'run'
            group by metric_key
            """
        )
        self.con.execute(
            """
            update dq.baselines b
            set mean = l.mean, sd = l.sd, n_runs = l.n_runs,
                last_value = l.last_value, updated_at = current_timestamp
            from _learned l
            where b.metric_key = l.metric_key
            """
        )
        return self.con.execute("select count(*) from dq.baselines").fetchone()[0]
