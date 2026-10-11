"""Does the suite actually catch the faults it claims to catch?

The fixture is generated first (`etl/synthesize.py`), so these tests measure the
monitor against a file whose every published number is true - and then against
versions of that file with exactly one known fault in them. A check that never
fires on any injected incident is indistinguishable from a check that cannot fire.

The injections run once per session and both assertions read that run, because a
full pass costs about a minute and measuring the same bytes twice proves nothing.
"""

from __future__ import annotations

import csv

import pytest

from etl import synthesize
from monitor import incidents

RUNS_BEFORE_JUDGING = incidents.WARM_RUNS


@pytest.fixture(scope="session")
def fixture_dir() -> dict:
    manifest = synthesize.write(incidents.FIXTURE_DIR)
    assert manifest["measured"]["counts"]["trajectories"] == 3_000
    return manifest


@pytest.fixture(scope="session")
def registry(fixture_dir) -> list:
    return incidents.registry()


@pytest.fixture(scope="session")
def injected(registry, tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("incidents")
    return {
        incident.id: incidents.run_incident(incident, base / incident.id, registry)
        for incident in incidents.INCIDENTS
    }


def test_clean_fixture_satisfies_every_check_it_publishes(fixture_dir, registry, tmp_path):
    """The baseline the detection score is measured against: zero findings, five runs."""
    control = incidents.control(RUNS_BEFORE_JUDGING + 2, tmp_path, registry)
    assert control["checks"] == len(registry)
    assert control["judged_runs"] == 2
    assert control["findings"] == [], [f["check_id"] for f in control["findings"]]
    assert control["flapping_checks"] == []


def test_card_numbers_are_measured_not_declared(fixture_dir):
    """What the card claims and what the bytes say must be the same arithmetic."""
    counts = fixture_dir["measured"]["counts"]
    for table, key in (("candidates", "candidates"), ("jobs", "jobs"), ("history", "trajectories")):
        with (incidents.FIXTURE_DIR / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == counts[key] == fixture_dir["files"][f"{table}.csv"]["rows"]

    events = sum(bool(row[stage]) for row in rows for stage in incidents.STAGES)
    assert events == counts["events"]


@pytest.mark.incident
@pytest.mark.parametrize("incident", incidents.INCIDENTS, ids=lambda i: i.id)
def test_injected_incident_moves_the_checks_it_should(incident, injected):
    """Detection, not just activity: the named checks, not merely some check."""
    result = injected[incident.id]
    assert not result["missed"], (
        f"{incident.id} injected but these stayed green: {result['missed']}\n"
        f"fired instead: {[f['check_id'] for f in result['findings']]}"
    )


@pytest.mark.incident
@pytest.mark.parametrize("incident", incidents.INCIDENTS, ids=lambda i: i.id)
def test_a_critical_incident_pages_and_a_warning_one_does_not(incident, injected):
    """The alerting contract, measured on the same injections as detection.

    The no-page direction is asserted too: an alert suite that pages on every
    warning trains its readers to ignore it when something is actually broken.
    """
    result = injected[incident.id]
    if incident.must_page:
        assert result["paged"], f"{incident.id} broke something critical without paging"
    else:
        assert not result["paged"], (
            f"{incident.id} is a warning-level fault but routed "
            f"{result['paged']} page(s): {result['page_subjects']}"
        )
