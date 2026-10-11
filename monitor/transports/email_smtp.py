"""SMTP adapter for the alert mail.

Same discipline as the Slack adapter: everything comes from the environment, the
connection is injectable, and the test suite drives it with a stub instead of a mail
server. Python removed `smtpd`, so there is no stdlib sink to stand up here - a stub
connection that records the message it was handed tests exactly the same code path up
to the socket.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable, Protocol

from monitor.alerts import AlertMessage
from monitor.transports.base import Receipt


class _Connection(Protocol):
    def send_message(self, message: EmailMessage) -> object: ...
    def close(self) -> None: ...


ENV = {
    "host": "DQ_SMTP_HOST",
    "port": "DQ_SMTP_PORT",
    "user": "DQ_SMTP_USER",
    "password": "DQ_SMTP_PASSWORD",
    "sender": "DQ_ALERT_FROM",
    "recipients": "DQ_ALERT_TO",
}


@dataclass
class SmtpTransport:
    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    timeout: float = 10.0
    connector: Callable[[], _Connection] | None = field(default=None, repr=False)
    name: str = "email"

    @classmethod
    def from_env(cls, env: dict) -> "SmtpTransport | None":
        host, recipients = env.get(ENV["host"]), env.get(ENV["recipients"])
        if not host or not recipients:
            return None
        to = tuple(address.strip() for address in recipients.split(",") if address.strip())
        return cls(
            host=host,
            port=int(env.get(ENV["port"], "587")),
            sender=env.get(ENV["sender"], "data-monitor@localhost"),
            recipients=to,
            username=env.get(ENV["user"]) or None,
            password=env.get(ENV["password"]) or None,
        )

    def build(self, subject: str, text: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(text)
        return message

    def _open(self) -> _Connection:
        if self.connector is not None:
            return self.connector()
        server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        if self.starttls:
            server.starttls()
        if self.username:
            server.login(self.username, self.password or "")
        return server

    def send(self, message: AlertMessage) -> Receipt:
        built = self.build(message.subject, message.text)
        try:
            connection = self._open()
        except (OSError, smtplib.SMTPException) as exc:
            return Receipt(self.name, False, f"connect failed: {exc}")
        try:
            refused = connection.send_message(built)
        except (OSError, smtplib.SMTPException) as exc:
            return Receipt(self.name, False, f"send failed: {exc}")
        finally:
            try:
                connection.close()
            except Exception:
                pass
        if refused:
            return Receipt(self.name, False, f"refused by server: {', '.join(refused)}")
        return Receipt(self.name, True, f"delivered to {len(self.recipients)} recipient(s)")
