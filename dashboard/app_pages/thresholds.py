"""Thresholds: what "normal" means, and how the monitor arrived at it."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import queries
from monitor.thresholds import MIN_HISTORY, SIGMA

st.caption(
    f"These are not typed into a config file. Each run records its metrics to "
    f"`dq.metrics`; the monitor then judges a metric against `±{SIGMA:g} × standard "
    f"deviation` from its own history, floored at the check's minimum tolerance so a "
    f"metric that has never moved cannot produce a zero-width band. Below "
    f"{MIN_HISTORY} runs there is no distribution to learn from, so the check says it "
    "is seeding instead of guessing."
)

rows = queries.baselines()
if rows.empty:
    st.info(
        "No learned thresholds yet. Run the monitor a few times - the first runs "
        "seed the baselines that later runs judge themselves against.",
        icon=":material/hourglass_top:",
    )
    st.stop()

flat = rows[rows["sd"] == 0]
seeding = rows[~rows["judging"]]

with st.container(horizontal=True):
    st.metric("Metrics tracked", f"{len(rows):,}", border=True)
    st.metric("Judged against history", f"{len(rows) - len(seeding):,}", border=True)
    st.metric("Still seeding", f"{len(seeding):,}", border=True)
    st.metric("Band set by the floor, not by sd", f"{len(flat):,}", border=True)
    st.metric("Widest band", f"{rows['band'].max():,.4g}", border=True)

if len(flat):
    st.info(
        f"**{len(flat)} of {len(rows)} metrics have never moved** across "
        "the recorded runs, so their standard deviation is 0 and the band you see is "
        "the check's minimum tolerance doing its job. Against a byte-identical "
        "re-landing that is the honest answer: there is no evidence of normal variation "
        "yet, and a zero-width band would page on the third decimal place.",
        icon=":material/info:",
    )

table = rows.assign(
    status=rows["judging"].map({True: "judging", False: "seeding"}),
)[["metric_key", "status", "n_runs", "mean", "sd", "min_tol", "band", "last_value"]]
st.dataframe(
    table,
    hide_index=True,
    height=380,
    alt="Learned baselines: mean, standard deviation, tolerance floor and resulting band per metric",
)

choice = st.selectbox(
    "Inspect a metric",
    list(rows["metric_key"]),
    key="metric_choice",
    help="Charts every value this metric has recorded, with the band it will be judged against.",
)
row = rows[rows["metric_key"] == choice].iloc[0]
history = queries.metric_history(choice)

with st.container(border=True):
    st.subheader(choice, anchor=False)
    mean, band = float(row["mean"]), float(row["band"])
    series = history.assign(
        upper=mean + band,
        # Not floored at zero: a band that would extend below zero is exactly what the
        # minimum tolerance exists to stop, and clipping the line would hide that.
        lower=mean - band,
        mean=mean,
    )
    if len(series) == 1:
        series = pd.concat([series, series], ignore_index=True)
    st.line_chart(
        series,
        x="recorded_at",
        y=["value", "mean", "lower", "upper"],
        height=300,
        alt=f"Recorded values for {choice} against its learned band",
    )
    with st.container(horizontal=True):
        st.metric("Runs recorded", f"{int(row['n_runs']):,}", border=True)
        st.metric("Mean", f"{mean:,.4g}", border=True)
        st.metric("Std dev", f"{float(row['sd']):,.4g}", border=True)
        st.metric("Min tolerance", f"{float(row['min_tol']):,.4g}", border=True)
        st.metric("Band", f"±{band:,.4g}", border=True)
        st.metric("Latest", f"{float(row['last_value']):,.4g}", border=True)
    if float(row["sd"]) == 0:
        st.caption(
            f"Band is `max({SIGMA:g} × sd, min_tol)`. sd is 0 for this metric - every "
            "recorded run landed the same source bytes and the number never moved - so "
            "the band you see is the check's own floor, not a fitted range. A week of "
            "genuinely changing extracts is what turns it into one."
        )
    else:
        st.caption(
            f"Band is `max({SIGMA:g} × sd, min_tol)` = ±{band:,.4g} around a mean of "
            f"{mean:,.4g} fitted from {int(row['n_runs'])} recorded runs."
        )
