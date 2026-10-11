"""Always-available transport: render the alert where the run is running."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from monitor.alerts import AlertMessage
from monitor.transports.base import Receipt


@dataclass
class ConsoleTransport:
    name: str = "console"
    sink: Callable[[str], object] | None = field(default=None)

    def send(self, message: AlertMessage) -> Receipt:
        out = self.sink or print
        out(f"\n=== ALERT [{message.severity.upper()}] {message.subject} ===\n{message.text}")
        return Receipt(self.name, True, "rendered locally")
