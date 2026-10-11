"""Health: does the run we just recorded look like the runs before it?"""

from __future__ import annotations

import streamlit as st

import queries

run_id = st.session_state.get("selected_run") or queries.latest_run_id()
this_run = queries.run_row(run_id)
if this_run.empty:
    st.error(f"No run `{run_id}` in the database.")
    st.stop()
run = this_run.iloc[0]

history = queries.runs(limit=120).sort_values("started_at")
previous = history[history["started_at"] < run["started_at"]]
prev_row = previous.iloc[-1] if not previous.empty else None

results = queries.results(run_id)
failures = results[results["status"].isin(["fail", "error"])]

# --- KPI row -----------------------------------------------------------------

with st.container(horizontal=True):
    st.metric("Checks judged", f"{run.checks_total:,}", border=True)
    st.metric(
        "Findings",
        f"{run.failed + run.errored:,}",
        delta=None if prev_row is None else int(run.failed + run.errored - prev_row.failed - prev_row.errored),
        delta_color="inverse",
        delta_description="vs previous run",
        border=True,
    )
    st.metric(
        "Warnings",
        f"{run.warned:,}",
        delta=None if prev_row is None else int(run.warned - prev_row.warned),
        delta_color="inverse",
        delta_description="vs previous run",
        border=True,
    )
    st.metric("Critical findings", f"{int((failures['severity'] == 'critical').sum()):,}", border=True)
    st.metric("Alerts sent", f"{run.alerts_sent:,}", border=True)
    st.metric("Runtime", f"{run.wall_ms:,} ms", border=True)

# A partial run is the one trap a health page can fall into: 120 passes out of 151
# checks is not a healthy run, it is a run that did not look at 31 things.
fullest = int(history["checks_total"].max())
if run.checks_total < fullest:
    st.warning(
        f"**Incomplete run.** This one judged {run.checks_total} of "
        f"{fullest} checks - the fullest run on record. The {fullest - run.checks_total} "
        "unmeasured checks are not passes; the alert router deliberately refuses to "
        "close an incident on a run that did not test it.",
        icon=":material/warning:",
    )

col_left, col_right = st.columns([3, 2])

with col_left, st.container(border=True):
    st.subheader("Findings by check family", anchor=False)
    by_family = queries.status_by_family(run_id)
    by_family["family label"] = by_family["family"].map(queries.family_label)
    st.bar_chart(
        by_family,
        x="family label",
        y=["fails", "errors"],
        horizontal=True,
        sort=False,
        height=300,
        alt="Failed and errored checks per family for this run",
    )
    st.dataframe(
        by_family[["family label", "total", "passes", "warns", "fails", "errors"]],
        hide_index=True,
        height="auto",
        alt="Check counts per family",
    )

with col_right, st.container(border=True):
    st.subheader("Findings across runs", anchor=False)
    trend = history.tail(30)[["started_at", "failed", "errored", "warned"]].copy()
    st.line_chart(
        trend,
        x="started_at",
        y=["failed", "errored", "warned"],
        height=300,
        alt="Failed, errored and warned check counts over recent runs",
    )
    st.caption(
        "A rising line is either a real data change or a check that has started to "
        "disagree with itself - the Findings page separates the two."
    )

# --- what changed ------------------------------------------------------------

if prev_row is not None:
    prev_results = queries.results(prev_row["run_id"])
    prev_status = dict(zip(prev_results["check_id"], prev_results["status"]))
    became = results[
        (results["status"].isin(["fail", "error"]))
        & (~results["check_id"].isin(prev_status.keys()))
    ]
    worsened = results.merge(
        prev_results[["check_id", "status"]], on="check_id", suffixes=("", "_prev")
    )
    worsened = worsened[
        (worsened["status"].isin(["fail", "error"]))
        & (~worsened["status_prev"].isin(["fail", "error"]))
    ]
    calmed = results.merge(
        prev_results[["check_id", "status"]], on="check_id", suffixes=("", "_prev")
    )
    calmed = calmed[
        (calmed["status_prev"].isin(["fail", "error"]))
        & (calmed["status"] == "pass")
    ]
    with st.container(border=True):
        st.subheader(f"What changed since {prev_row.run_id}", anchor=False)
        with st.container(horizontal=True):
            st.metric("New checks", f"{len(became):,}", border=True)
            st.metric("Newly failing", f"{len(worsened):,}", border=True)
            st.metric("Recovered", f"{len(calmed):,}", border=True)
        if became.empty and worsened.empty:
            st.caption("No new checks and nothing newly failing since the previous run.")
        for label, frame in (("New checks", became), ("Newly failing", worsened)):
            if not frame.empty:
                with st.expander(f"{label} ({len(frame)})"):
                    st.dataframe(
                        frame[["check_id", "family", "severity", "status", "detail"]],
                        hide_index=True,
                        alt=f"{label} compared with the previous run",
                    )

# --- provenance --------------------------------------------------------------

with st.container(border=True):
    st.subheader("Source bytes behind this run", anchor=False)
    st.caption(
        f"Snapshot fingerprint `{run.snapshot_sha}` - the hash of the landed tables "
        "this run judged. Two runs with the same fingerprint saw the same bytes."
    )
    try:
        landed = queries.manifest()
    except Exception as exc:
        st.info(f"No landing manifest in this database: {exc}")
    else:
        landed = landed.assign(
            sha256=landed["sha256"].str.slice(0, 16) + "…",
            bytes=landed["bytes"].map(lambda b: f"{int(b):,}"),
            rows=landed["rows"].map(lambda r: f"{int(r):,}"),
        )
        st.dataframe(
            landed[["source_file", "rows", "bytes", "sha256", "loaded_at", "run_id"]],
            hide_index=True,
            alt="Landed source files with row counts, byte sizes and checksums",
        )
