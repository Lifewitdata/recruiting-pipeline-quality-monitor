"""Alert routing: what pages, how often, and what reaches the wire.

Everything here runs offline. The Slack adapter POSTs to a loopback HTTP sink in
`mock_sink.py`; the SMTP adapter is handed a stub connection that records the message
it was given. No workspace, no mailbox, no credentials.
"""

from __future__ import annotations

import datetime as dt

import pytest

from monitor.alerts import Policy, build_message, route, routes
from monitor.checks.base import ERROR, FAIL, PASS, WARN
from monitor.drilldown import Drilldown
from monitor.transports import configure, deliver
from monitor.transports.base import Receipt
from monitor.transports.console import ConsoleTransport
from monitor.transports.email_smtp import SmtpTransport
from monitor.transports.slack import SlackWebhookTransport
from tests.helpers import make_check, make_outcome, make_report
from tests.mock_sink import MockSink


def state(con, check_id: str) -> dict:
    result = con.execute("select * from dq.alerts where check_id = ?", [check_id])
    columns = [d[0] for d in result.description]
    rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
    assert len(rows) == 1, rows
    return rows[0]


# --- routing policy ---------------------------------------------------------


@pytest.mark.parametrize(
    "severity,status,expected",
    [
        ("critical", FAIL, True),
        ("critical", ERROR, True),  # the monitor failing to measure is itself an outage
        ("warning", ERROR, True),
        ("warning", FAIL, False),  # a dashboard item, not a page
        ("info", FAIL, False),
        ("critical", PASS, False),
        ("critical", WARN, False),
    ],
)
def test_only_page_worthy_findings_reach_a_transport(severity, status, expected):
    assert routes(make_outcome(make_check(severity=severity), status=status)) is expected


# --- dedup + cooldown -------------------------------------------------------


def test_a_failure_that_stays_broken_pages_once_then_waits(monitor_db):
    """Four runs of the same outage cost one page, while the incident counts up."""
    policy = Policy(cooldown_runs=4)
    sent = []
    for run_id in ("run-1", "run-2", "run-3", "run-4", "run-5"):
        routing = route(monitor_db, make_report([make_outcome()], run_id=run_id), policy)
        assert routing.suppressed == (0 if routing.to_send else 1)
        sent.extend(routing.to_send)

    assert [d.message.event for d in sent] == ["opened", "repeated"]
    assert [d.message.run_id for d in sent] == ["run-1", "run-5"]
    row = state(monitor_db, "funnel.accepted_without_proposed")
    assert (row["occurrences"], row["notifications"], row["runs_since_notify"]) == (5, 2, 0)
    assert "cooldown" in sent[1].message.text


def test_the_hour_gate_pages_even_when_the_run_gate_has_not_arrived(monitor_db):
    """A weekly job has to page the next morning, not wait for four more runs."""
    policy = Policy(cooldown_runs=99)
    route(monitor_db, make_report([make_outcome()], run_id="run-1"), policy)
    yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
    monitor_db.execute("update dq.alerts set last_notified_at = ?", [yesterday])

    routing = route(monitor_db, make_report([make_outcome()], run_id="run-2"), policy)
    assert [d.message.event for d in routing.to_send] == ["repeated"]


# --- the incident state machine --------------------------------------------


def test_clearing_a_failure_closes_the_incident_and_a_recurrence_reopens_it(monitor_db):
    failing = make_outcome()
    passing = make_outcome(failing.check, status=PASS, observed=0.0)

    route(monitor_db, make_report([failing], run_id="run-1"), Policy())
    closed = route(monitor_db, make_report([passing], run_id="run-2"), Policy())
    assert [d.message.event for d in closed.dispatches] == ["resolved"]
    assert state(monitor_db, failing.check.id)["status"] == "resolved"
    assert "passing again" in closed.dispatches[0].message.text

    again = route(monitor_db, make_report([failing], run_id="run-3"), Policy())
    assert [d.message.event for d in again.to_send] == ["reopened"]
    row = state(monitor_db, failing.check.id)
    # a recurrence is news, so its counters start over instead of inheriting history
    assert (row["occurrences"], row["notifications"]) == (1, 1)


def test_a_resolved_incident_stays_quiet_when_resolve_notices_are_off(monitor_db):
    failing = make_outcome()
    route(monitor_db, make_report([failing], run_id="run-1"), Policy())
    closed = route(
        monitor_db,
        make_report([make_outcome(failing.check, status=PASS)], run_id="run-2"),
        Policy(notify_on_resolve=False),
    )
    assert closed.to_send == []
    assert closed.dispatches[0].suppressed is True
    assert state(monitor_db, failing.check.id)["status"] == "resolved"


def test_a_partial_run_leaves_incidents_it_never_measured_alone(monitor_db):
    """`--family trend` is one slice of the registry; it must not close the rest."""
    funnel_failure = make_outcome()
    trend_failure = make_outcome(make_check("trend.volume_last_month.events", kind="close"))
    route(monitor_db, make_report([funnel_failure, trend_failure], run_id="run-1"), Policy())

    partial = route(
        monitor_db,
        make_report([make_outcome(trend_failure.check, status=PASS, observed=0.0)], "run-2"),
        Policy(),
    )
    assert [d.message.event for d in partial.dispatches] == ["resolved"]
    assert state(monitor_db, funnel_failure.check.id)["status"] == "open"
    assert state(monitor_db, funnel_failure.check.id)["occurrences"] == 1


# --- what the alert says ----------------------------------------------------


def test_the_alert_carries_the_rows_behind_the_finding(monitor_db):
    routing = route(monitor_db, make_report([make_outcome()], run_id="run-9"), Policy())
    text = routing.to_send[0].message.text
    assert "29160" in text and "Drill-down" in text and "run `run-9`" in text


def test_a_broken_drill_down_reports_itself_without_hiding_the_finding(monitor_db):
    broken = make_outcome(make_check(drilldown="select * from stg.table_that_was_never_loaded"))
    routing = route(monitor_db, make_report([broken], run_id="run-9"), Policy())
    dispatch = routing.to_send[0]
    assert dispatch.message.event == "opened"
    assert "drill-down failed" in dispatch.message.text
    # the incident is still recorded, with the failure noted in the stored body
    assert "drill-down failed" in state(monitor_db, broken.check.id)["body"]


def test_a_check_with_nothing_to_measure_still_alerts(monitor_db):
    errored = make_outcome(
        make_check(drilldown=""), status=ERROR, observed=None, detail="query failed: Binder"
    )
    routing = route(monitor_db, make_report([errored], run_id="run-9"), Policy())
    text = routing.to_send[0].message.text
    assert "not measurable" in text and "Binder" in text


# --- transports: real sockets, fake endpoints --------------------------------


def test_the_slack_adapter_posts_the_finding_to_the_sink(monitor_db):
    routing = route(monitor_db, make_report([make_outcome()], run_id="run-7"), Policy())
    with MockSink([200]) as sink:
        receipts = deliver(routing, [SlackWebhookTransport(sink.url)])

    assert receipts[0].ok is True and receipts[0].dispatched == 1
    posted = sink.last
    assert (posted.method, posted.content_type) == ("POST", "application/json")
    payload = posted.json()
    assert payload["text"].startswith(":rotating_light:")
    attachment = payload["attachments"][0]
    assert attachment["color"] == "#B4231C"
    assert "funnel.accepted_without_proposed" in attachment["text"]
    assert "run run-7" in attachment["footer"]


def test_a_rejected_payload_is_a_failed_receipt_not_a_crash(monitor_db):
    routing = route(monitor_db, make_report([make_outcome()], run_id="run-8"), Policy())
    with MockSink([500]) as sink:
        receipt = SlackWebhookTransport(sink.url).send(routing.to_send[0].message)
    assert receipt.ok is False and "HTTP 500" in receipt.detail
    assert len(sink.messages) == 1  # it tries exactly once, retries are the scheduler's job


def test_an_endpoint_that_is_not_listening_is_reported_not_raised():
    transport = SlackWebhookTransport("http://127.0.0.1:1/nothing-here", timeout=1.0)
    message = build_message(
        make_outcome(), "run-x", "opened", Drilldown("x", [], []), "snapshot", 1
    )
    receipt = transport.send(message)
    assert receipt.ok is False and "transport error" in receipt.detail


def test_a_long_alert_is_clipped_to_what_slack_accepts(monitor_db):
    routing = route(
        monitor_db, make_report([make_outcome(detail="x" * 9000)], run_id="run-9"), Policy()
    )
    with MockSink([200]) as sink:
        deliver(routing, [SlackWebhookTransport(sink.url)])
    body = sink.last.json()["attachments"][0]["text"]
    assert len(body) <= 3000 and body.endswith("…")


def test_the_email_adapter_builds_a_message_without_a_mail_server(monitor_db):
    sent: list[object] = []
    closed: list[bool] = []

    class StubConnection:
        def send_message(self, message):
            sent.append(message)
            return []  # nothing refused

        def close(self):
            closed.append(True)

    routing = route(monitor_db, make_report([make_outcome()], run_id="run-6"), Policy())
    transport = SmtpTransport(
        host="127.0.0.1",
        port=587,
        sender="data-monitor@example.invalid",
        recipients=("analytics@example.invalid", "oncall@example.invalid"),
        connector=StubConnection,
    )
    receipt = transport.send(routing.to_send[0].message)

    assert receipt.ok is True and "2 recipient(s)" in receipt.detail
    assert closed == [True], "the connection closes even when the send succeeds"
    message = sent[0]
    assert message["Subject"] == routing.to_send[0].message.subject
    assert "analytics@example.invalid" in message["To"]
    assert "funnel.accepted_without_proposed" in message.get_content()


def test_a_send_failure_still_closes_the_connection(monitor_db):
    held: list[list[bool]] = []

    class ExplodingConnection:
        def __init__(self):
            self.flags = [False]
            held.append(self.flags)

        def send_message(self, message):
            raise OSError("connection reset")

        def close(self):
            self.flags[0] = True

    transport = SmtpTransport(
        host="127.0.0.1",
        port=25,
        sender="a@example.invalid",
        recipients=("b@example.invalid",),
        connector=lambda: ExplodingConnection(),
    )
    routing = route(monitor_db, make_report([make_outcome()], run_id="run-6"), Policy())
    receipt = transport.send(routing.to_send[0].message)
    assert receipt.ok is False and "send failed" in receipt.detail
    assert held[0][0] is True


# --- wiring -----------------------------------------------------------------


def test_one_dead_transport_does_not_stop_the_others(monitor_db):
    routing = route(monitor_db, make_report([make_outcome()], run_id="run-5"), Policy())
    rendered: list[str] = []
    dead = SlackWebhookTransport("http://127.0.0.1:1/gone", timeout=1.0)
    receipts = deliver(routing, [dead, ConsoleTransport(sink=rendered.append)])

    assert [r.ok for r in receipts] == [False, True]
    assert receipts[1].dispatched == 1
    assert "ALERT [CRITICAL]" in rendered[0]


def test_a_suppressed_alert_never_reaches_any_transport(monitor_db):
    policy = Policy(cooldown_runs=4)
    route(monitor_db, make_report([make_outcome()], run_id="run-1"), policy)
    quiet = route(monitor_db, make_report([make_outcome()], run_id="run-2"), policy)
    assert quiet.to_send == []

    with MockSink([200]) as sink:
        receipts = deliver(quiet, [SlackWebhookTransport(sink.url)])
    assert sink.messages == []
    assert receipts[0].dispatched == 0 and receipts[0].ok is True


def test_transports_switch_on_only_when_their_environment_says_so():
    assert [t.name for t in configure({})] == ["console"]
    assert [t.name for t in configure({"SLACK_WEBHOOK_URL": "http://127.0.0.1:1/x"})] == [
        "console",
        "slack",
    ]
    assert [t.name for t in configure({"DQ_SMTP_HOST": "smtp.example.invalid"})] == ["console"]
    mail = configure(
        {"DQ_SMTP_HOST": "smtp.example.invalid", "DQ_ALERT_TO": "oncall@example.invalid"}
    )
    assert [t.name for t in mail] == ["console", "email"]


def test_receipts_summarise_themselves_for_the_run_log():
    assert str(Receipt("slack", True, "HTTP 200: ok", 3)) == "slack: sent 3 alert(s) - HTTP 200: ok"
    assert "FAILED" in str(Receipt("email", False, "connect refused"))
