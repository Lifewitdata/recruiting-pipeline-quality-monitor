"""Which findings page, how often, and what the page says.

Two rules do most of the work here:

1. Only a critical failure (or any check that could not run at all) is routed. A
   warning belongs in a dashboard someone reads on Monday; a critical failure belongs
   in a channel that interrupts someone. Routing everything is the same as routing
   nothing, and the first team to get paged for a rounding slip is the last team to
   read the channel.
2. A finding that stays broken produces one alert. The fingerprint is the check plus
   its scope, so a six-day outage costs six notifications only if the cooldown is
   shorter than a day - otherwise the row counts up quietly and the page says so.

The state that makes (2) work lives in `dq.alerts`, not in memory: the policy has to
survive the process ending between runs, and in a real deployment the nightly job and
the person acknowledging the alert are not the same machine.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from monitor.checks.base import ERROR, FAIL, Outcome
from monitor.db import utcnow
from monitor.drilldown import Drilldown
from monitor.drilldown import query as drill


@dataclass(frozen=True)
class Policy:
    cooldown_runs: int = 4  # suppress repeats for this many runs...
    cooldown_hours: float = 12.0  # ...or this many hours, whichever comes first
    notify_on_resolve: bool = True
    max_drilldown_rows: int = 6


@dataclass(frozen=True)
class AlertMessage:
    fingerprint: str
    event: str  # opened | repeated | resolved
    subject: str
    text: str
    check_id: str
    severity: str
    run_id: str

    def as_slack(self) -> dict:
        return {
            "text": f"[{self.severity.upper()}] {self.subject}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{self.subject}*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": self.text}},
            ],
        }


@dataclass
class Dispatch:
    message: AlertMessage
    occurrences: int
    notifications: int
    suppressed: bool = False


@dataclass
class Routing:
    dispatches: list[Dispatch] = field(default_factory=list)

    @property
    def to_send(self) -> list[Dispatch]:
        return [d for d in self.dispatches if not d.suppressed]

    @property
    def suppressed(self) -> int:
        return sum(1 for d in self.dispatches if d.suppressed)

    @property
    def resolved(self) -> int:
        return sum(1 for d in self.dispatches if d.message.event == "resolved")


def fingerprint(check_id: str, scope: str) -> str:
    """Stable incident identity: the same check on the same scope is one incident."""
    return hashlib.sha1(f"{check_id}|{scope}".encode()).hexdigest()[:16]


def routes(outcome: Outcome) -> bool:
    """Pager eligibility. A check that cannot run is an outage of the monitor itself."""
    if outcome.status == ERROR:
        return True
    return outcome.status == FAIL and outcome.check.severity == "critical"


def _due(before: dict | None, now, policy: Policy) -> tuple[bool, str, int]:
    """(send?, event, runs since the last notification)."""
    if before is None or before["status"] != "open":
        return True, ("reopened" if before else "opened"), 1
    since = (before["runs_since_notify"] or 0) + 1
    last = before["last_notified_at"]
    hours = (now - last).total_seconds() / 3600.0 if last else float("inf")
    return since >= policy.cooldown_runs or hours >= policy.cooldown_hours, "repeated", since


def _direction(observed: float | None, expected: float | None) -> str:
    if observed is None:
        return "not measurable"
    if expected is None:
        return f"measured {observed:,.4g}"
    return f"{observed:,.4g} against an expectation of {expected:,.4g}"


def _plain(value: str) -> str:
    return value.replace("`", "'")


def build_message(
    outcome: Outcome,
    run_id: str,
    event: str,
    findings: Drilldown,
    snapshot: str,
    occurrences: int,
) -> AlertMessage:
    check = outcome.check
    verb = {"opened": "NEW", "reopened": "BACK", "repeated": "STILL", "resolved": "CLEARED"}[
        event
    ]
    subject = f"{verb} {check.severity}: {check.name}"

    if event == "resolved":
        text = (
            f"*{check.id}* is passing again (scope `{check.scope}`).\n"
            f"{check.description}\n\n"
            f"_it failed on {occurrences} run(s) before clearing; the incident is closed "
            "in `dq.alerts`._"
        )
    else:
        lead = (
            f"*{check.id}* ({check.family} family, scope `{check.scope}`)\n"
            f"{check.description}\n\n"
            f"_status_: {outcome.status} - {_direction(outcome.observed, outcome.expected)} "
            f"{check.unit}"
        )
        if outcome.detail:
            lead += f"\n`{_plain(outcome.detail)}`"
        if event == "repeated":
            lead += (
                f"\n\n_failed on {occurrences} consecutive runs; this page is the cooldown "
                "expiry, not a new problem_"
            )
        text = "\n".join(
            [
                lead,
                "",
                f"_Drill-down, top {len(findings.rows)}:_ {findings.error or ''}".strip(),
                findings.as_markdown(),
                "",
                f"run `{run_id}` - snapshot `{snapshot or 'n/a'}`",
            ]
        )

    return AlertMessage(
        fingerprint=fingerprint(check.id, check.scope),
        event=event,
        subject=subject,
        text=text,
        check_id=check.id,
        severity=check.severity,
        run_id=run_id,
    )


def _prior(con) -> dict[str, dict]:
    result = con.execute("select * from dq.alerts")
    columns = [d[0] for d in result.description]
    return {row[0]: dict(zip(columns, row, strict=True)) for row in result.fetchall()}


def _write(con, fp: str, state: dict, message: AlertMessage) -> None:
    con.execute(
        """
        insert or replace into dq.alerts
            (fingerprint, check_id, scope, severity, status, first_seen, last_seen,
             occurrences, notifications, last_notified_at, runs_since_notify, subject, body)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            fp,
            state["check_id"],
            state["scope"],
            state["severity"],
            state["status"],
            state["first_seen"],
            state["last_seen"],
            state["occurrences"],
            state["notifications"],
            state["last_notified_at"],
            state["runs_since_notify"],
            message.subject,
            message.text,
        ],
    )


def route(con, report, policy: Policy = Policy()) -> Routing:
    """Reconcile the run against open incidents, persist the new state, say what to send.

    Checks this run did not evaluate are left exactly as they are: a `--family` run is
    a partial view of the data and must not close someone else's incident.
    """
    now = utcnow()
    known = _prior(con)
    routing = Routing()

    for outcome in report.outcomes:
        check = outcome.check
        fp = fingerprint(check.id, check.scope)
        before = known.get(fp)

        if routes(outcome):
            was_open = before is not None and before["status"] == "open"
            occurrences = (before["occurrences"] or 0) + 1 if was_open else 1
            due, event, since = _due(before, now, policy)
            findings = drill(con, check, policy.max_drilldown_rows)
            message = build_message(
                outcome, report.run_id, event, findings, report.snapshot_sha, occurrences
            )
            notifications = ((before["notifications"] or 0) if was_open else 0) + (1 if due else 0)
            routing.dispatches.append(
                Dispatch(message, occurrences, notifications, suppressed=not due)
            )
            state = {
                "check_id": check.id,
                "scope": check.scope,
                "severity": check.severity,
                "status": "open",
                "first_seen": before["first_seen"] if was_open else now,
                "last_seen": now,
                "occurrences": occurrences,
                "notifications": notifications,
                "last_notified_at": now if due else (before or {}).get("last_notified_at"),
                "runs_since_notify": 0 if due else since,
            }
            _write(con, fp, state, message)
            continue

        if before is not None and before["status"] == "open":
            message = build_message(
                outcome,
                report.run_id,
                "resolved",
                Drilldown(check.id, [], []),
                report.snapshot_sha,
                before["occurrences"] or 0,
            )
            routing.dispatches.append(
                Dispatch(
                    message,
                    before["occurrences"] or 0,
                    (before["notifications"] or 0) + int(policy.notify_on_resolve),
                    suppressed=not policy.notify_on_resolve,
                )
            )
            _write(
                con,
                fp,
                {
                    **before,
                    "status": "resolved",
                    "last_seen": now,
                },
                message,
            )

    return routing
