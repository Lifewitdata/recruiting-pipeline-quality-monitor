"""Data-health triage console.

Entry point for `streamlit run dashboard/streamlit_app.py`. This is not a BI
dashboard: it does not describe the business, it describes whether the numbers the
business dashboard shows can be trusted, and where to start digging when they cannot.
"""

from __future__ import annotations

import streamlit as st

import queries

st.set_page_config(page_title="Data health", page_icon=":material/monitor_heart:", layout="wide")

if not queries.available():
    st.title("Data health", icon=":material/monitor_heart:")
    st.warning(f"No monitor database at `{queries.DB_PATH}`.")
    st.markdown(
        "Land the extract and judge it first, then come back:\n\n"
        "```\npython -m etl.load\npython -m monitor.cli run\n```"
    )
    st.stop()

# Newest first, so the default selection is the run a triage session actually starts
# from. The label carries the run's own counts - the console never restates a number
# it has not read.
history = queries.runs(limit=120)
labels = {
    row.run_id: (
        f"{row.started_at:%Y-%m-%d %H:%M}  ·  {row.checks_total} checks"
        f"  ·  {row.failed} fail  ·  {row.errored} error"
    )
    for row in history.itertuples()
}
run_ids = list(history["run_id"])

with st.sidebar:
    st.header("Triage", icon=":material/filter_alt:")
    st.selectbox(
        "Monitor run",
        run_ids,
        index=0,
        key="selected_run",
        format_func=lambda r: labels.get(r, r),
    )
    st.divider()
    st.caption(
        f"{queries.run_count()} runs recorded. Every figure here is read from the "
        "monitor's own `dq` tables - the same rows the nightly job wrote."
    )
    st.link_button(
        "Dataset card (upstream)",
        "https://zenodo.org/records/21390581",
    )

page = st.navigation(
    [
        st.Page("app_pages/health.py", title="Health", icon=":material/monitor_heart:"),
        st.Page("app_pages/findings.py", title="Findings", icon=":material/report:"),
        st.Page("app_pages/thresholds.py", title="Thresholds", icon=":material/show_chart:"),
        st.Page("app_pages/alerts.py", title="Alerts", icon=":material/notifications:"),
        st.Page("app_pages/detection.py", title="Detection", icon=":material/safety_check:"),
    ],
    position="top",
)

st.title(page.title, icon=page.icon)
page.run()
