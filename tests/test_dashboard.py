"""The console renders against a database the monitor itself populated.

These are headless render tests (`streamlit.testing`), not browser tests: they prove
every page executes against real `dq` rows without raising, which is the failure mode
that actually bites - a page that dies on an empty table, on a null timestamp, or on a
column the engine stopped writing.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
PAGES = ("health", "findings", "thresholds", "alerts", "detection")

for _path in (str(ROOT), str(DASHBOARD)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture(scope="session")
def console(tmp_path_factory):
    """Land the synthetic fixture, run the registry clean, then break it once.

    Both states are needed on purpose. A console only ever tested against a green run
    cannot be trusted to show a red one, and the empty-state branches are where the
    null-handling bugs live.
    """
    from etl import synthesize
    from monitor import alerts, engine, incidents

    synthesize.write(incidents.FIXTURE_DIR)

    work = tmp_path_factory.mktemp("console")
    db_path = work / "monitor.duckdb"
    data = incidents.copy_fixture(work)
    con = incidents.connect(db_path)
    incidents.land_all(con, data, "clean-land")
    checks = incidents.registry()

    # Distinct, ordered timestamps: four runs inside one second would make "the
    # previous run" undefined, and that comparison is what the Health page is built on.
    base = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def stamp(report, index):
        report.started_at = base + dt.timedelta(minutes=index)
        report.finished_at = report.started_at + dt.timedelta(seconds=3)
        return report

    for i in range(4):
        engine.persist(con, stamp(engine.run(con, checks, f"clean-{i}"), i), alerts_sent=0)

    # One injected incident, through the same path the harness uses, so the console is
    # rendering a real finding with a real stored drill-down, not a stub.
    incident = incidents.INCIDENTS[0]
    incident.in_file(data)
    incidents.land_all(con, data, "broken")
    broken = stamp(engine.run(con, checks, "broken"), 4)
    routing = alerts.route(con, broken, alerts.Policy())
    engine.persist(con, broken, alerts_sent=len(routing.to_send))
    con.close()

    import queries

    queries.DB_PATH = db_path
    for cached in (queries.runs, queries.results, queries.baselines, queries.alerts):
        cached.clear()
    yield queries
    for cached in (queries.runs, queries.results, queries.baselines, queries.alerts):
        cached.clear()


def render(page: str) -> AppTest:
    """Run one page file headlessly.

    The module-level `sys.path` inserts above stand in for what Streamlit does for
    real: the entry point's directory is on the path, so `import queries` resolves the
    same way it does when the app runs.
    """
    at = AppTest.from_file(DASHBOARD / "app_pages" / f"{page}.py", default_timeout=180)
    at.run()
    return at


def exceptions(at: AppTest) -> list[str]:
    return [str(getattr(e, "value", e)) for e in at.exception]


ELEMENTS = ("metric", "dataframe", "caption", "info", "warning", "success", "markdown",
            "code", "subheader", "selectbox", "multiselect", "segmented_control")


def rendered(at: AppTest) -> int:
    """A script that fails to compile raises nothing and draws nothing, so "no
    exception" alone is not proof the page ran."""
    return sum(len(getattr(at, name)) for name in ELEMENTS)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_raising(page, console):
    at = render(page)
    assert not exceptions(at), f"{page}.py raised: {exceptions(at)}"
    assert rendered(at) > 0, f"{page}.py produced no elements at all"


def test_health_shows_the_run_it_just_judged(console):
    at = render("health")
    # The KPI row is six cards. Fewer means a query silently returned nothing and the
    # page drew zeroes instead of saying so.
    assert len(at.metric) >= 6, [m.label for m in at.metric]
    assert len(at.dataframe) >= 1


def test_findings_lists_the_injected_incident(console):
    at = render("findings")
    assert len(at.dataframe) >= 1
    assert "contract.row_count.history" in str(at.dataframe[0].value)


def test_thresholds_comes_from_the_same_formula_the_engine_judges_with(console):
    at = render("thresholds")
    frame = console.baselines()
    assert not frame.empty
    assert (frame["band"] >= frame["min_tol"]).all(), "a band below its own floor is a bug"
    assert (frame["n_runs"] >= 1).all()
    assert len(at.metric) >= 4


def test_alerts_shows_the_incident_the_router_opened(console):
    at = render("alerts")
    assert not exceptions(at)
    assert not console.alerts("all").empty, "the injected critical should have opened an incident"


def test_detection_reads_the_harness_report(console):
    at = render("detection")
    assert console.incident_report() is not None, "run python -m monitor.incidents first"
    assert len(at.metric) >= 4, [m.label for m in at.metric]


def test_every_published_drill_down_actually_runs(console):
    """The console's promise: the SQL that finds the rows is stored with the finding.

    A drill-down that only fails when someone is triaging at midnight is worse than no
    drill-down, so this executes every query the registry published, not just the one
    attached to a current failure.
    """
    results = console.results(console.latest_run_id())
    published = sorted(set(results["drilldown_sql"].dropna()) - {""})
    assert len(published) >= 20, f"only {len(published)} drill-downs in the registry"

    broken = {}
    for sql in published:
        _, error = console.drilldown(sql)
        if error:
            broken[sql[:80]] = error
    assert not broken, f"{len(broken)} drill-down(s) cannot run: {broken}"


def test_a_finding_drill_down_returns_rows(console):
    results = console.results(console.latest_run_id())
    findings = results[results["status"].isin(["fail", "error"])]
    runnable = findings[findings["drilldown_sql"].fillna("") != ""]
    assert not runnable.empty

    frame, error = console.drilldown(runnable.iloc[0]["drilldown_sql"])
    assert error == "", error
    assert frame is not None and len(frame) <= 50

    broken, why = console.drilldown("select * from a_table_that_does_not_exist")
    assert broken is None and "does not exist" in why

    none, note = console.drilldown("")
    assert none is None and "no drill-down" in note
