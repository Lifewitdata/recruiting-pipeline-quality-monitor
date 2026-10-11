"""Alert transports: one interface, three implementations, no live workspace.

`Transport.send()` takes the same `AlertMessage` a human would read, so the routing
logic above the transport is tested against the same object graph production uses -
only the last hop differs.

Nothing here contacts a real Slack workspace or mail server. The Slack adapter POSTs
to whatever URL `SLACK_WEBHOOK_URL` holds, and the test suite points it at a local
HTTP sink on 127.0.0.1 and asserts on the bytes that arrive. The SMTP adapter takes
an injectable connection for the same reason. Set the environment variables in a real
deployment and the same code goes live; until then the README says so plainly.
"""

from __future__ import annotations

import os

from monitor.transports.base import Receipt, Transport

__all__ = ["Receipt", "Transport", "deliver", "configure"]


def deliver(routing, transports: list) -> list[Receipt]:
    """Fan the due alerts out. One transport failing does not stop the others."""
    receipts: list[Receipt] = []
    for transport in transports:
        sent, first_failure = 0, None
        for dispatch in routing.to_send:
            try:
                receipt = transport.send(dispatch.message)
            except Exception as exc:  # a dead webhook must not kill the run
                receipt = Receipt(transport.name, False, f"{type(exc).__name__}: {exc}")
            if receipt.ok:
                sent += 1
            elif first_failure is None:
                first_failure = receipt.detail
        receipts.append(
            Receipt(transport.name, first_failure is None, first_failure or "all delivered", sent)
        )
    return receipts


def configure(env: dict | None = None) -> list[Transport]:
    """Build the transport list from the environment.

    Console is always on so a run without secrets still produces a readable alert.
    The rest switch on when their configuration is present, which is what lets the
    same entry point run in CI (console only) and in production (Slack + email).
    """
    env = dict(os.environ if env is None else env)
    from monitor.transports import console, email_smtp, slack

    transports: list[Transport] = [console.ConsoleTransport()]
    if env.get("SLACK_WEBHOOK_URL"):
        transports.append(slack.SlackWebhookTransport(env["SLACK_WEBHOOK_URL"]))
    mail = email_smtp.SmtpTransport.from_env(env)
    if mail is not None:
        transports.append(mail)
    return transports
