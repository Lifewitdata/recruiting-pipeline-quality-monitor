"""The check registry.

Every module here contributes checks; the engine runs the flat list. A new check
is a declarative instance, never a new code path in the engine.
"""

from __future__ import annotations

from monitor.checks.base import ERROR, FAIL, PASS, WARN, Check, Outcome, col

__all__ = ["Check", "Outcome", "col", "build_registry", "PASS", "WARN", "FAIL", "ERROR"]


def build_registry(claims: dict | None = None) -> list[Check]:
    from monitor.checks import contract, format, funnel, reconcile, structure, trend

    checks: list[Check] = []
    checks += contract.build(claims)
    checks += structure.build()
    checks += funnel.build(claims)
    checks += format.build(claims)
    checks += reconcile.build(claims)
    checks += trend.build(claims)
    return checks
