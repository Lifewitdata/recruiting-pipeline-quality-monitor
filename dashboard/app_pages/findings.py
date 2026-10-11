"""Findings: pick a check, read what it saw, and run its own root-cause query."""

from __future__ import annotations

import math

import streamlit as st

import queries


def _num(value) -> str:
    # A rate of 41.5535% and a row count of 727 should not both print with the same
    # six decimals: show counts as integers, rates at four significant figures.
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    number = float(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.4g}"


run_id = st.session_state.get("selected_run") or queries.latest_run_id()
all_results = queries.results(run_id)

families = sorted(all_results["family"].unique())
with st.container(horizontal=True):
    st.segmented_control(
        "Show",
        ["Findings", "Warnings", "Every check"],
        default="Findings",
        key="scope",
    )
    st.multiselect("Family", families, default=families, key="families")
    st.text_input(
        "Search",
        placeholder="check id, name or detail",
        key="search",
        label_visibility="collapsed",
    )

scope = st.session_state.get("scope", "Findings")
if scope == "Findings":
    view = all_results[all_results["status"].isin(["fail", "error"])]
elif scope == "Warnings":
    view = all_results[all_results["status"] == "warn"]
else:
    view = all_results.copy()

chosen_families = st.session_state.get("families", families)
view = view[view["family"].isin(chosen_families)]
search = (st.session_state.get("search") or "").strip().lower()
if search:
    haystack = (
        view["check_id"].fillna("").str.lower()
        + " "
        + view["name"].fillna("").str.lower()
        + " "
        + view["detail"].fillna("").str.lower()
    )
    view = view[haystack.str.contains(search, regex=False)]

view = view.reset_index(drop=True)
st.caption(
    f"{len(view):,} of {len(all_results):,} checks shown. Select a row to open its "
    "detail, drill-down query and history across runs."
)

if view.empty:
    st.success(
        "Nothing matches these filters. On a healthy run that is "
        "the expected state of this page - it is not evidence the checks ran; the "
        "Health page shows how many were judged.",
        icon=":material/task_alt:",
    )
    st.stop()

event = st.dataframe(
    view[
        [
            "check_id",
            "family",
            "severity",
            "status",
            "observed",
            "expected",
            "unit",
            "detail",
        ]
    ],
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key=f"select_{run_id}_{scope}_{len(chosen_families)}_{search}",
    height=360,
    alt="Check results for the selected run",
)

rows = event.selection.rows
if not rows:
    st.info(
        "Select a row to triage it. The drill-down query is the check's own SQL, "
        "written when the check was defined and stored with the result - so the "
        "answer to 'why did this fail' is never a guess made later.",
        icon=":material/ads_click:",
    )
    st.stop()

row = view.iloc[rows[0]]

st.divider()
with st.container(border=True):
    st.subheader(row["check_id"], anchor=False)
    st.markdown(f"**{row['name']}**")
    st.caption(row["description"])
    with st.container(horizontal=True):
        st.metric("Status", str(row["status"]))
        st.metric("Severity", str(row["severity"]))
        st.metric("Observed", _num(row["observed"]))
        st.metric("Expected", _num(row["expected"]))
        st.metric("Tolerance", _num(row["tolerance"]))
        st.metric("Unit", str(row["unit"] or "—"))
        st.metric("Check took", f"{int(row['duration_ms'] or 0):,} ms")
    st.write(f"`{queries.family_label(row['family'])}` · scope `{row['scope']}`")
    st.markdown(f":orange[{row['detail']}]")

with st.container(border=True):
    st.subheader("Root cause", anchor=False)
    sql = row["drilldown_sql"] or ""
    st.code(sql, language="sql")
    frame, error = queries.drilldown(sql)
    if error:
        st.error(error, icon=":material/error:")
    elif frame is None or frame.empty:
        st.success("The drill-down returned no rows - for a check that counts bad "
                   "records, an empty result on a passing run is what this looks like.",
                   icon=":material/search_off:")
    else:
        st.dataframe(frame, hide_index=True, alt=f"Rows identified by the drill-down query for {row['check_id']}")
        st.caption("Up to 50 rows, read from the same warehouse the check queried.")

with st.container(border=True):
    st.subheader("Across runs", anchor=False)
    history = queries.result_history(row["check_id"], limit=40).sort_values("started_at")
    if history.empty:
        st.caption("No history for this check.")
    else:
        st.line_chart(
            history,
            x="started_at",
            y=["observed", "expected"],
            height=280,
            alt=f"Observed versus expected for {row['check_id']} across runs",
        )
        st.dataframe(
            history[["run_id", "started_at", "status", "observed", "expected", "detail"]],
            hide_index=True,
            height=220,
            alt=f"Run-by-run history for {row['check_id']}",
        )
