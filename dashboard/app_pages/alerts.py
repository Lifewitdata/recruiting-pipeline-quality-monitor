"""Alerts: what the router chose to page about, and proof that the cooldown holds."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import queries
from monitor.alerts import Policy

# The two cooldown windows are imported, never retyped: if the router's policy moves,
# this page's arithmetic moves with it instead of quietly describing a rule no longer in
# force.
policy = Policy()

st.caption(
    "Alert state is global - it lives in the monitor's `dq.alerts` table and carries "
    "across runs, so this page is not scoped to the run selected in the sidebar. It "
    "shows what the router interrupted someone about, which incidents are still open, "
    "and whether the cooldown is holding a repeat back."
)

choice = st.segmented_control(
    "Status",
    ["Open", "Resolved", "All"],
    default="Open",
    key="alert_status",
)
frame = queries.alerts((choice or "Open").lower())

if frame.empty:
    st.info(
        "No incidents match this filter. An empty alert table is not the same as "
        "'nothing is wrong': the router only writes a row for a check that is eligible "
        "to page (a critical failure, or a check that could not run at all), and it "
        "never *closes* an open incident on a run that did not test it. A partial "
        "`--family` run leaves the other incidents exactly as they were, so an incident "
        "can still be open here even though the last run said nothing about it. Switch "
        "to `All` to see resolved incidents too.",
        icon=":material/search_off:",
    )
    st.stop()

open_frame = frame[frame["status"] == "open"]

# Whole days between first_seen and last_seen, measured only across still-open incidents.
if open_frame.empty:
    longest_open_days = 0
else:
    ages = (open_frame["last_seen"] - open_frame["first_seen"]).dt.days
    longest_open_days = int(ages.max())

median_since = frame["runs_since_notify"].median()

# --- KPI row -----------------------------------------------------------------

with st.container(horizontal=True):
    st.metric("Incidents", f"{len(frame):,}", border=True)
    st.metric("Still open", f"{len(open_frame):,}", border=True)
    st.metric("Times paged", f"{int(frame['notifications'].sum()):,}", border=True)
    st.metric("Times observed", f"{int(frame['occurrences'].sum()):,}", border=True)
    st.metric(
        "Median runs since last page",
        "—" if pd.isna(median_since) else f"{median_since:g}",
        border=True,
    )
    st.metric("Longest open incident", f"{longest_open_days} day{'s' if longest_open_days != 1 else ''}", border=True)

# --- the cooldown, in the numbers this table actually holds --------------------

with st.container(border=True):
    st.subheader("How the cooldown works", anchor=False)
    busiest = frame.loc[int(frame["occurrences"].idxmax())]
    seen = int(busiest["occurrences"])
    paged = int(busiest["notifications"])
    st.write(
        f"`occurrences` counts how many runs this incident was seen on; "
        f"`notifications` counts how many times it actually paged. The busiest incident "
        f"here, `{busiest['check_id']}`, has been seen {seen} "
        f"{'time' if seen == 1 else 'times'} but paged only {paged} "
        f"{'time' if paged == 1 else 'times'}. Being seen more often than it pages is "
        "the cooldown working, not a lost alert - the row counts up quietly while the "
        "page stays quiet."
    )
    st.caption(
        f"The router re-pages an already-open incident only once either "
        f"{policy.cooldown_runs} runs have passed since the last notification or "
        f"{policy.cooldown_hours:g} hours have elapsed, whichever comes first "
        "(`Policy.cooldown_runs` / `Policy.cooldown_hours` in `monitor/alerts.py`). "
        "One identity - the check plus its scope - therefore stays one incident, however "
        "many runs it survives."
    )

# --- proof, two ways: per incident, then per run ------------------------------

with st.container(border=True):
    st.subheader("Seen versus paged, per incident", anchor=False)
    per_incident = frame.assign(
        incident=frame["check_id"] + " · " + frame["scope"].fillna("")
    )
    st.bar_chart(
        per_incident,
        x="incident",
        y=["occurrences", "notifications"],
        horizontal=True,
        sort=False,
        height=320,
        alt="Times each incident was observed versus how many times it paged, by check and scope",
    )
    st.caption(
        "A bar where the observed count runs ahead of the paged count is the cooldown "
        "doing its job: the fault kept happening, the monitor kept noticing, and it only "
        "interrupted once."
    )

with st.container(border=True):
    st.subheader("Findings versus pages, across runs", anchor=False)
    recent_runs = queries.runs(limit=30).sort_values("started_at")
    volume = recent_runs.assign(
        findings=recent_runs["failed"] + recent_runs["errored"],
    )[["started_at", "findings", "alerts_sent"]]
    st.bar_chart(
        volume,
        x="started_at",
        y=["findings", "alerts_sent"],
        height=240,
        alt="Failed and errored checks versus alerts actually sent, for each recent run",
    )
    st.caption(
        f"The nightly job records findings every run, but `alerts_sent` stays small: "
        f"{int(recent_runs['alerts_sent'].sum()):,} pages have gone out across the last "
        f"{len(recent_runs):,} runs. A persistent fault is one incident, not one page per "
        "run - this is the 'one incident, not 600 alerts' claim measured directly."
    )

# --- the alert rows -----------------------------------------------------------

st.caption("Select a row to read the incident and the exact page the router sent.")

event = st.dataframe(
    frame[
        [
            "check_id",
            "scope",
            "severity",
            "status",
            "first_seen",
            "last_seen",
            "occurrences",
            "notifications",
            "runs_since_notify",
            "subject",
        ]
    ],
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key=f"select_alerts_{(choice or 'Open').lower()}_{len(frame)}",
    height=360,
    alt="Alert incidents: check, scope, severity, status, first and last seen, occurrences, notifications and subject",
)

rows = event.selection.rows
if not rows:
    st.info(
        "Select an incident above to read its full message, fingerprint and timing. The "
        "`subject` column already shows the verb the router chose: NEW opens an "
        "incident, STILL is a cooldown expiry on one that never cleared, BACK is a "
        "re-occurrence after it had resolved, and CLEARED closes it.",
        icon=":material/ads_click:",
    )
    st.stop()

row = frame.iloc[rows[0]]
runs_since = row["runs_since_notify"]

st.divider()
with st.container(border=True):
    st.subheader(str(row["check_id"]), anchor=False)
    st.markdown(f"**{row['subject']}**")
    with st.container(horizontal=True):
        st.metric("Status", str(row["status"]))
        st.metric("Severity", str(row["severity"]))
        st.metric("Times seen", f"{int(row['occurrences']):,}")
        st.metric("Times paged", f"{int(row['notifications']):,}")
        st.metric(
            "Runs since last page",
            "—" if pd.isna(runs_since) else f"{int(runs_since):,}",
        )
    st.caption(
        f"Fingerprint `{row['fingerprint']}` · scope `{row['scope']}` · first seen "
        f"{row['first_seen']:%Y-%m-%d %H:%M} · last seen {row['last_seen']:%Y-%m-%d %H:%M}"
    )
    st.markdown("**The page the router sent**")
    st.markdown(row["body"])
