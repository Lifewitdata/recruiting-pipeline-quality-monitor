"""Turn a failing check into something a person can act on.

A check row says `format.zipcode_leading_zero_lost.candidates = 855`. That is not a
diagnosis. This module runs the check's own drill-down query and renders the top
rows, so the alert that reaches a human carries the handful of keys needed to start
the root-cause investigation without another query.

The drill-down is stored per check rather than written ad hoc because the point of
the registry is that the investigation is part of the test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from monitor.checks.base import Check

MAX_ROWS = 6


@dataclass
class Drilldown:
    check_id: str
    headers: list[str]
    rows: list[list[object]]
    error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.rows

    def cells(self, cell_width: int = 24) -> list[list[str]]:
        return [
            [self._clip(self._render(value), cell_width) for value in row] for row in self.rows
        ]

    def as_table(self, column_gap: str = " | ") -> str:
        if self.error:
            return f"drill-down failed: {self.error}"
        if self.empty:
            return "drill-down returned no rows"
        cells = self.cells()
        widths = [len(header) for header in self.headers]
        for row in cells:
            for i, value in enumerate(row):
                widths[i] = max(widths[i], len(value))
        lines = [
            column_gap.join(header.ljust(widths[i]) for i, header in enumerate(self.headers)),
            "-+-".join("-" * width for width in widths),
        ]
        lines += [column_gap.join(value.ljust(widths[i]) for i, value in enumerate(row))
                  for row in cells]
        return "\n".join(lines)

    def as_markdown(self) -> str:
        if self.error:
            return f"_drill-down failed: {self.error}_"
        if self.empty:
            return "_drill-down returned no rows_"
        lines = [
            "| " + " | ".join(self.headers) + " |",
            "| " + " | ".join("---" for _ in self.headers) + " |",
        ]
        lines += ["| " + " | ".join(row) + " |" for row in self.cells(cell_width=40)]
        return "\n".join(lines)

    @staticmethod
    def _render(value: object) -> str:
        """Month buckets and dates read better without a midnight time suffix."""
        if value is None:
            return "-"
        if isinstance(value, dt.datetime):
            midnight = (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
            return value.date().isoformat() if midnight and not value.tzinfo else value.isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        if isinstance(value, float):
            return f"{value:,.4g}"
        return str(value)

    @staticmethod
    def _clip(value: str, width: int) -> str:
        return value if len(value) <= width else value[: width - 1] + "…"


def query(con, check: Check, max_rows: int = MAX_ROWS) -> Drilldown:
    """Run the check's drill-down. A broken drill-down must never mask the finding."""
    if not check.drilldown:
        return Drilldown(check.id, [], [], error="this check publishes no drill-down query")
    try:
        result = con.execute(f"select * from (\n{check.drilldown}\n) limit {int(max_rows)}")
        rows = result.fetchall()
        headers = [d[0] for d in result.description]
    except Exception as exc:  # the finding stands on its own
        return Drilldown(check.id, [], [], error=f"{type(exc).__name__}: {exc}")
    return Drilldown(check.id, headers, [list(r) for r in rows])
