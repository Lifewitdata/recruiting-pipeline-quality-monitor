"""The check specification every module in this package fills in.

A check is declarative on purpose: one SQL statement that measures something, a
comparison rule, a severity, and a drill-down query that lists the offending rows.
Keeping them as data means the engine, the dashboard, the alert router and the
incident-injection scorer all work on any check without knowing what it measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PASS, WARN, FAIL, ERROR = "pass", "warn", "fail", "error"

# Comparison rules. `equal`/`zero`/`lte`/`close` judge against an expectation the
# check itself carries; `drift` judges against the baseline previous runs wrote.
KINDS = ("equal", "zero", "lte", "close", "drift", "info")


def col(name: str) -> str:
    """Quote an identifier. Several source columns start with a digit."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Check:
    id: str
    family: str
    scope: str
    name: str
    severity: str  # info | warning | critical
    kind: str
    description: str
    sql: str  # must return one row: observed [, expected , detail]
    unit: str = "count"
    expected: float | None = None  # comparison target for equal/lte/close
    tolerance: float | None = None  # band for close/drift
    fail_beyond: float = 3.0  # multiple of the band that escalates warn -> fail
    metric_key: str | None = None  # history key for drift checks
    drilldown: str = ""  # lists the rows behind a failure
    reference: str = ""  # where the expectation came from
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.id}: unknown kind {self.kind!r}")
        if self.severity not in ("info", "warning", "critical"):
            raise ValueError(f"{self.id}: unknown severity {self.severity!r}")


@dataclass
class Outcome:
    check: Check
    status: str
    observed: float | None
    expected: float | None
    tolerance: float | None
    detail: str
    duration_ms: int
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in (WARN, FAIL, ERROR)
