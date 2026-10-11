"""Command line entry point: fetch, load, run, report.

`run` is the scheduled job. It lands nothing and downloads nothing - it audits
whatever is in the DuckDB landing and writes a run row, so the nightly workflow
is fetch -> load -> run and each step can fail loudly on its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import db as dbmod  # noqa: E402
from monitor.alerts import Policy, Routing, route as route_alerts  # noqa: E402
from monitor.card import parse as parse_card  # noqa: E402
from monitor.checks import build_registry  # noqa: E402
from monitor.checks.base import ERROR, FAIL, PASS, WARN, Check  # noqa: E402
from monitor.drilldown import query as drill  # noqa: E402
from monitor.engine import RunReport, persist, run as run_checks  # noqa: E402
from monitor.transports import configure as configure_transports, deliver  # noqa: E402

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "% null":
        return f"{value:.2f}%"
    if unit in ("days", "hours", "ms"):
        return f"{value:,.1f}{unit}"
    return f"{value:,.4g}"


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(f"  {line}")
    for r in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def summarise(report: RunReport) -> None:
    counts = report.by_status()
    print(
        f"\n{len(report.outcomes)} checks: "
        f"{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[ERROR]} error"
        f"  ({report.wall_ms} ms, snapshot {report.snapshot_sha or 'n/a'})"
    )
    findings = [o for o in report.outcomes if o.status in (WARN, FAIL, ERROR)]
    if not findings:
        print("  nothing failed - every check reconciled to its expectation")
        return
    print(f"\nfindings ({len(findings)}):")
    findings.sort(key=lambda o: (SEVERITY_ORDER[o.check.severity], o.status != FAIL, o.check.id))
    print_table(
        [
            [
                o.check.severity,
                o.status,
                o.check.id,
                fmt(o.observed, o.check.unit),
                fmt(o.expected, o.check.unit),
                (o.detail or "")[:58],
            ]
            for o in findings
        ],
        ["severity", "status", "check", "observed", "expected", "detail"],
    )


def show_drilldown(con, checks: list[Check], check_id: str) -> int:
    """Re-open the last run's finding and print the rows behind it."""
    matches = [c for c in checks if c.id == check_id]
    if not matches:
        print(f"unknown check id: {check_id} - `python monitor/cli.py list` shows the registry")
        return 2
    check = matches[0]
    finding = con.execute(
        """
        select run_id, status, observed, expected, detail
        from dq.results where check_id = ?
        order by result_id desc limit 1
        """,
        [check_id],
    ).fetchone()
    print(f"\n{check.id}  [{check.family} / {check.severity} / {check.kind}]")
    print(f"  {check.name}\n  {check.description}")
    if finding is None:
        print("  no stored result - run the check first: python monitor/cli.py run")
        return 0
    run_id, status, observed, expected, detail = finding
    print(
        f"  last seen: {status} - observed {fmt(observed, check.unit)} vs expected "
        f"{fmt(expected, check.unit)} on {run_id}\n  {detail}"
    )
    if not check.drilldown:
        print("  this check publishes no drill-down query")
        return 0
    print("\n  top rows behind the finding:")
    table = drill(con, check, max_rows=20)
    print(_indent(table.as_table()))
    if table.error:
        return 0
    return 0


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def show_alerts(con) -> int:
    rows = con.execute(
        """
        select check_id, severity, status, occurrences, notifications,
               datediff('hour', coalesce(last_notified_at, first_seen), last_seen)
        from dq.alerts order by status = 'open' desc, occurrences desc, check_id
        """
    ).fetchall()
    if not rows:
        print("no incidents recorded - nothing has failed on a monitored check yet")
        return 0
    print_table(
        [
            [
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                "-" if r[5] is None else f"{r[5]}h since alert",
            ]
            for r in rows
        ],
        ["check", "severity", "status", "runs failing", "alerts sent", "quiet for"],
    )
    open_rows = [r for r in rows if r[2] == "open"]
    print(f"\n{len(rows)} incident(s), {len(open_rows)} open")
    return 0


def report_alerts(routing, receipts) -> None:
    """Print what the run decided to page about, including what the cooldown held back."""
    if not routing.dispatches:
        return
    print(f"\nalerts: {len(routing.to_send)} routed, {routing.suppressed} held by cooldown")
    for dispatch in routing.dispatches:
        state = "suppressed" if dispatch.suppressed else "sent"
        print(
            f"  [{state:<10}] {dispatch.message.event:<9} {dispatch.message.check_id} "
            f"(failing {dispatch.occurrences} run(s), {dispatch.notifications} alert(s) sent)"
        )
    for receipt in receipts:
        print(f"  {receipt}")


def main() -> int:
    ap = argparse.ArgumentParser(description="JTH recruiting funnel data-quality monitor")
    ap.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "list", "summary", "drill", "alerts"],
        help="run the checks (default), list the registry, print recent runs, "
        "drill into one finding, or show open incidents",
    )
    ap.add_argument("check_id", nargs="?", default=None, help="check id for `drill`")
    ap.add_argument("--family", help="restrict to one check family")
    ap.add_argument("--check", action="append", default=[], help="run only these check ids")
    ap.add_argument("--run-id", default=None)
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a critical check fails (CI uses this after a documented baseline)",
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    ap.add_argument(
        "--no-alerts",
        action="store_true",
        help="skip alert routing and delivery; the incident harness scores detection from "
        "dq.results, so it can inject failures without paging anyone",
    )
    args = ap.parse_args()

    con = dbmod.connect()
    claims = parse_card()
    checks = build_registry(claims)
    if args.family:
        checks = [c for c in checks if c.family == args.family]
    if args.check:
        wanted = set(args.check)
        checks = [c for c in checks if c.id in wanted]
        missing = wanted - {c.id for c in checks}
        if missing:
            print(f"unknown check ids: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.command == "list":
        for c in sorted(checks, key=lambda c: (c.family, c.id)):
            print(f"{c.family:<10} {c.severity:<8} {c.kind:<6} {c.id}")
        print(f"\n{len(checks)} checks registered")
        return 0

    if args.command == "summary":
        rows = con.execute(
            """
            select run_id, started_at, checks_total, passed, warned, failed, errored, alerts_sent
            from dq.runs order by started_at desc limit 15
            """
        ).fetchall()
        if not rows:
            print("no runs recorded yet")
            return 0
        print_table(
            [[r[0], str(r[1])[:19], r[2], r[3], r[4], r[5], r[6], r[7]] for r in rows],
            ["run_id", "started_at", "total", "pass", "warn", "fail", "error", "alerts"],
        )
        return 0

    if args.command == "alerts":
        return show_alerts(con)

    missing = [
        r[0]
        for r in con.execute(
            """
            select t from (
                values ('raw.candidates'), ('raw.jobs'), ('raw.history')
            ) as v(t)
            where not exists (
                select 1 from information_schema.tables
                where table_schema = split_part(t, '.', 1) and table_name = split_part(t, '.', 2)
            )
            """
        ).fetchall()
    ]
    if missing:
        print(
            f"landing is empty ({', '.join(missing)} missing) - run:\n"
            "  python etl/fetch.py\n  python etl/load.py",
            file=sys.stderr,
        )
        return 2

    if args.command == "drill":
        if not args.check_id:
            print("`drill` needs a check id, e.g. python monitor/cli.py drill "
                  "format.zipcode_canonical.candidates", file=sys.stderr)
            return 2
        return show_drilldown(con, checks, args.check_id)

    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    started = dbmod.utcnow()
    report = run_checks(con, checks, run_id)
    report.started_at = started
    report.finished_at = dbmod.utcnow()

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "snapshot": report.snapshot_sha,
                    "summary": report.by_status(),
                    "checks": [
                        {
                            "id": o.check.id,
                            "family": o.check.family,
                            "severity": o.check.severity,
                            "status": o.status,
                            "observed": o.observed,
                            "expected": o.expected,
                            "tolerance": o.tolerance,
                            "unit": o.check.unit,
                            "detail": o.detail,
                        }
                        for o in report.outcomes
                    ],
                },
                indent=2,
            )
        )
    else:
        summarise(report)

    # Alerts are routed before the results are written: the routing reads the previous
    # run's incident state, and a run that pages must record how many pages it sent.
    routing, receipts = Routing(), []
    if not args.no_alerts:
        routing = route_alerts(con, report, Policy())
        receipts = deliver(routing, configure_transports())
        if not args.json:
            report_alerts(routing, receipts)

    persist(con, report, alerts_sent=len(routing.to_send))
    con.close()
    print(f"\nwrote dq.results + dq.runs for {run_id}")
    if args.strict and report.critical_failures:
        return 1
    return 1 if report.by_status()[ERROR] else 0


if __name__ == "__main__":
    raise SystemExit(main())
