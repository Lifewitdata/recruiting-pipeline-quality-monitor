"""The contract every transport implements.

Kept in its own module so an adapter can import it without importing the package
that discovers the adapters - the registry in `__init__` imports the adapters lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from monitor.alerts import AlertMessage


@dataclass
class Receipt:
    transport: str
    ok: bool
    detail: str
    dispatched: int = 0

    def __str__(self) -> str:
        state = "sent" if self.ok else "FAILED"
        return f"{self.transport}: {state} {self.dispatched} alert(s) - {self.detail}"


class Transport(Protocol):
    name: str

    def send(self, message: AlertMessage) -> Receipt: ...
