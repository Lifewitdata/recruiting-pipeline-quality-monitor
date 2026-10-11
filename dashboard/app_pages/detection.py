"""Detection: how the check suite performs against deliberately injected incidents."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import queries

report = queries.incident_report()
if report is None:
    st.warning(
        "No incident report at `reports/incident_report.json`, so there is nothing "
        "measured on this page yet. The figures here come from the incident-injection "
        "harness, which you run to generate the report. It is invoked as a module, "
        "not as a `monitor.cli` subcommand:\n\n"
        "```\npython -m monitor.incidents\n```\n\n"
        "Add `--only <incident-id>` to re-run a single incident, or `--verbose` to "
        "print every finding it produced. The harness warms the learned thresholds, "
        "injects one known fault, re-runs the suite and scores what fired.",
        icon=":material/science:",
    )
    st.stop()

control = report["control"]
silent = report["incidents_that_should_page_but_did_not"]
over_paged = report["incidents_that_should_not_page_but_did"]

st.caption(
    f"Generated {report['generated_at']} against the `{report['fixture']}` fixture: "
    f"{report['checks_in_registry']:,} checks in the registry, with "
    f"{report['warm_runs_per_incident']} warm-up runs per incident so every drift check "
    "has a learned band to be judged against before a fault is injected."
)

# --- KPI row -----------------------------------------------------------------

with st.container(horizontal=True):
    st.metric("Incidents injected", f"{report['incidents']:,}", border=True)
    st.metric("Checks designed to fire", f"{report['checks_expected']:,}", border=True)
    st.metric("Checks that fired", f"{report['checks_detected']:,}", border=True)
    st.metric("Detection rate", f"{report['detection_rate_pct']:g}%", border=True)
    st.metric("Incidents that paged", f"{report['incidents_that_paged']:,}", border=True)
    st.metric("False-positive rate", f"{control['false_positive_rate']:g}%", border=True)

# --- the verdict, in one line -------------------------------------------------

st.caption(
    f"{report['incidents_fully_detected']:,} of {report['incidents']:,} incidents were "
    f"fully attributed to their designed checks, and {report['criticals_paged']:,} "
    f"alert(s) were routed across {report['incidents_that_paged']:,} of the "
    f"{report['incidents_needing_a_page']:,} incident(s) that needed a page. "
    f"{len(silent)} incident(s) that should have paged stayed silent; "
    f"{len(over_paged)} that should have stayed quiet paged anyway."
)

# --- per-incident table -------------------------------------------------------

table = pd.DataFrame(
    [
        {
            "id": r["id"],
            "layer": r["layer"],
            "story": r["story"],
            "expected": len(r["expected"]),
            "detected": len(r["detected"]),
            "missed": len(r["missed"]),
            "detected_share": r["detected_share"],
            "should_page": r["should_page"],
            "paged": r["paged"],
            "unattributed": len(r["unattributed"]),
        }
        for r in report["results"]
    ]
)

with st.container(border=True):
    st.subheader("Every incident", anchor=False)
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            "detected_share": st.column_config.NumberColumn(
                "detected share", format="%.1f%%"
            ),
        },
        height=380,
        alt="Per-incident detection results: designed, detected, missed and unattributed check counts and paging",
    )

# --- one incident, opened up --------------------------------------------------

by_id = {r["id"]: r for r in report["results"]}
choice = st.selectbox(
    "Inspect an incident",
    list(by_id),
    key="incident_choice",
    help="Each incident injects exactly one known fault, so expected versus detected is a check on the suite, not on the data.",
)
incident = by_id[choice]

with st.container(border=True):
    st.subheader(incident["id"], anchor=False)
    st.write(incident["story"])
    if incident["note"]:
        st.caption(incident["note"])

    with st.container(horizontal=True):
        st.metric("Layer", str(incident["layer"]))
        st.metric("Expected", f"{len(incident['expected']):,}")
        st.metric("Detected", f"{len(incident['detected']):,}")
        st.metric("Missed", f"{len(incident['missed']):,}")
        st.metric("Should page", "yes" if incident["should_page"] else "no")
        st.metric("Paged", f"{incident['paged']:,}")

    # Pills over the designed checks: this is the harness's ground truth, shown as the
    # fixed set the run is scored against rather than an interactive filter.
    st.pills(
        "Expected checks",
        incident["expected"],
        selection_mode="multi",
        default=incident["expected"],
        disabled=True,
        wrap=True,
        key=f"expected_{incident['id']}",
    )

    st.markdown("**Detected**")
    if incident["detected"]:
        st.markdown("\n".join(f"- `{c}`" for c in incident["detected"]))
    else:
        st.caption("None.")

    st.markdown("**Missed**")
    if incident["missed"]:
        st.error(
            "Missed: " + ", ".join(f"`{c}`" for c in incident["missed"]),
            icon=":material/error:",
        )
    else:
        st.success("No designed check was missed.", icon=":material/task_alt:")

    with st.container(border=True):
        st.markdown("**Findings this injected run produced**")
        st.dataframe(
            pd.DataFrame(incident["findings"]),
            hide_index=True,
            alt=f"Every non-passing check result for incident {incident['id']}",
        )

    with st.container(border=True):
        st.markdown(f"**Pages routed ({incident['paged']})**")
        if incident["page_subjects"]:
            st.markdown("\n".join(f"- {s}" for s in incident["page_subjects"]))
        else:
            st.caption(
                "Nothing was routed for this incident - either it is a warning-severity "
                "fault the router deliberately holds back from paging, or the cooldown "
                "kept a repeat quiet."
            )

    with st.container(border=True):
        st.markdown("**Unattributed findings**")
        if incident["unattributed"]:
            st.dataframe(
                pd.DataFrame(incident["unattributed"]),
                hide_index=True,
                alt=f"Checks that moved without being designed to, for incident {incident['id']}",
            )
            st.caption(
                "An unattributed finding is a check that moved when this injected "
                "incident was not designed to make it move. That is neither a pass to "
                "celebrate nor a false positive to dismiss: it is a finding about the "
                "suite - a detector with a wider blast radius than its definition "
                "claims - and worth reading before trusting the detection rate above."
            )
        else:
            st.caption(
                "Every check that moved here was one this incident was designed to move. "
                "A clean attribution like this is the result the harness is aiming for."
            )

# --- the control run ----------------------------------------------------------

with st.container(border=True):
    st.subheader("Control run", anchor=False)
    st.caption(
        "The same fixture with no fault injected, re-run until the learned bands exist "
        "and then judged. Anything that moves here moves on its own - that is the "
        "false-positive rate, and it is what gives the detection rate its meaning."
    )
    with st.container(horizontal=True):
        st.metric("Clean runs", f"{control['runs']:,}", border=True)
        st.metric("Judged runs", f"{control['judged_runs']:,}", border=True)
        st.metric("Checks", f"{control['checks']:,}", border=True)
        st.metric("Findings", f"{len(control['findings']):,}", border=True)
        st.metric("Flapping checks", f"{len(control['flapping_checks']):,}", border=True)
        st.metric("False-positive rate", f"{control['false_positive_rate']:g}%", border=True)

    if control["findings"]:
        st.dataframe(
            pd.DataFrame(control["findings"]),
            hide_index=True,
            alt="Findings that appeared on the clean, un-injected control runs",
        )
    if control["flapping_checks"]:
        st.markdown("**Checks that changed status between control runs**")
        st.markdown("\n".join(f"- `{c}`" for c in control["flapping_checks"]))
    if not control["findings"] and not control["flapping_checks"]:
        st.success(
            "The untouched fixture produced no findings and no flapping checks across "
            "the judged control runs.",
            icon=":material/verified:",
        )
