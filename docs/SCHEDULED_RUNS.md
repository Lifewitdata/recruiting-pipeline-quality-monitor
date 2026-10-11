# Scheduled runs

How the monitor is meant to run unattended, what each stage is allowed to fail on,
and what a human is supposed to do about it.

## The three commands

The schedule is three separate commands, not one wrapper, so a bad night names the
stage that broke:

| stage | command | fails when | does not fail when |
|---|---|---|---|
| acquire | `python etl/fetch.py` | a file is missing, or its bytes do not match the sha256 pinned in `etl/fetch.py` | upstream published something surprising - that is a finding, not a fetch error |
| land | `python etl/load.py` | a pinned file cannot be read into `raw.*`/`stg.*`, or the event log cannot be built | a column is full of nulls, a stage date moved |
| judge | `python monitor/cli.py run` | a check raised instead of judging (exit 1 on any `error` status) | checks failed - failures are rows in `dq.results`, and routing decides who hears about them |

`python monitor/cli.py run --strict` additionally exits 1 when a *critical* check
fails. The schedule does not use it: on the upstream JTH extract there are critical
findings every single run (they are the reason the project exists), so a strict badge
would be red permanently and therefore ignored. The gate on a monitor is routing, not
exit codes.

Optional flags: `--json` for machine-readable output, `--family trend` to restrict the
run, `--check <id>` to re-judge one check while you fix it, `--no-alerts` to score
detection without paging anybody (that is what the incident harness runs with).

## Timing

`.github/workflows/nightly-monitor.yml` runs at **06:00 UTC**. That is 22:00 the
previous evening in Dallas (America/Chicago) - after the agency's US recruiting day
has closed - and 11:30 in New Delhi, so the engineer reads the result at the start of
their own morning rather than at 03:00.

GitHub Actions caveats worth knowing before you rely on this:

- `schedule` only fires on the default branch.
- GitHub stops running cron workflows on a repository with no activity for 60 days.
  A monitor nobody has pushed to is a monitor nobody is being alerted by, which is a
  worse failure mode than a red badge. The nightly itself touches the repo, so a
  healthy schedule keeps itself alive only if something else pushes - do not count on
  it; the workflow has an `if: always()` summary step so a run is at least visible.
- `concurrency: nightly-monitor` with `cancel-in-progress: false` means a slow night
  waits instead of two runs racing to write the same DuckDB file.
- `timeout-minutes: 20`. A full judge pass on the real extract is measured in seconds;
  twenty minutes is headroom for the download, and a run that exceeds it is an outage
  you want to hear about.

On self-hosted or any other scheduler the same three commands are the contract -
systemd timers, cron, Airflow, Prefect, Dagster, all of them just call these:

```
0 6 * * *  cd /srv/recruiting-monitor && /usr/bin/flock -n .lock \
             bash -c 'python etl/fetch.py && python etl/load.py && python monitor/cli.py run'
```

`flock -n` because a previous run that never finished must not be joined by a second
writer on the same DuckDB.

## Environment

| variable | used by | default / behaviour when unset |
|---|---|---|
| `MONITOR_DUCKDB` | `etl/load.py`, `monitor/db.py`, `dashboard/queries.py` | `<repo>/db/monitor.duckdb`. Every component reads it, which is what lets CI, the nightly job and the console point at the same file - or at three different ones |
| `SLACK_WEBHOOK_URL` | `monitor/transports/slack.py` | unset → no Slack transport. Console output still happens |
| `DQ_SMTP_HOST` | `monitor/transports/email_smtp.py` | unset → no email transport (`from_env` returns `None`) |
| `DQ_SMTP_PORT` | email | `587`, STARTTLS |
| `DQ_SMTP_USER` / `DQ_SMTP_PASSWORD` | email | unset → no `login()` call, i.e. an unauthenticated relay |
| `DQ_ALERT_FROM` | email | `data-monitor@localhost` |
| `DQ_ALERT_TO` | email | unset → no email transport even with a host |

Console is **always** on (`monitor/transports/__init__.py: configure()`), so a run with
zero secrets still writes a readable alert into the job log. Nothing in this project
reads credentials from a file, and `secrets.toml` / `credentials.yml` are gitignored.

## What the router does with a finding

Rules implemented in `monitor/alerts.py`, verified by tests in `tests/`:

- Only a `fail` at `severity = critical` is routed, plus any check that raised
  (`error`), because a check that cannot run has no way to report anything.
- An incident is keyed by `fingerprint = sha1(check_id | scope)` truncated to 16 hex
  characters. The same check failing in two tables is two incidents; the same incident
  on consecutive nights is one incident with a rising `occurrences`.
- Dual cooldown: an incident re-pages after **4 runs** or **12 hours**, whichever comes
  first, and never while it is already open and inside both windows. So the incident
  that opens on Monday and is still broken on Friday pages twice, not sixty.
- A run that did not judge a check cannot close its incident. Partial runs are the
  trap: silently treating "not measured" as "fixed" is how an alerting system hides
  the thing it was paged about.
- Recovery pages once (`resolved`), then goes quiet.

Open incidents are visible without touching a secret: `python monitor/cli.py alerts`.

## Being honest about what is tested

The Slack and SMTP adapters are exercised against **local mock sinks** in
`tests/` (an `http.server` for the webhook, an injected connection for SMTP), asserting
the exact payload and the failure path when the sink returns 500 or refuses the
connection. That is what "delivery works" means in this repository. What has *not* been
done is sending a real message into a real workspace or mail server - the payload
shape, retries and receipts are proven; live-side quirks like channel rate limits are
not claimed anywhere.

The detection claim is measured, not asserted: `python -m monitor.incidents` injects
14 faults, and the report says which named checks fired, which did not, what else
moved that was not expected, and what the five clean control runs cost in false
positives. Read `reports/incident_report.json` after each change to the check registry.

## Triage order for a bad night

1. The console, `Health` page: is this run complete (`checks_total` vs the fullest run
   on record) and is the finding count new versus the previous run?
2. `Findings`: select the check. Read `detail`, then run its stored `drilldown_sql` -
   the rows behind the finding, from the same warehouse the check queried.
3. `Thresholds`: is this a band the monitor learned from three runs, or is the floor
   doing the work because the metric has never moved?
4. `Alerts`: has this already paged? `occurrences` against `notifications` tells you
   whether you are looking at a new incident or a twelve-day one you already know about.
5. Fix at the right layer. A `contract` family finding means the extract changed and the
   card is now wrong - that is a conversation with the producer. A `format` finding is
   usually a loader or cast problem. A `trend` finding with everything else green is a
   real business change, and the correct action is to let the baseline learn it.

## Run it locally

```bash
pip install -r requirements.txt -r requirements-dev.txt -r requirements-dashboard.txt
python etl/fetch.py            # download + verify the pinned checksums
python etl/load.py             # land raw -> stg -> stg.events
python monitor/cli.py run      # judge 151 checks, route what is critical
cd dashboard && streamlit run streamlit_app.py
```

CI never needs the real files: `python etl/synthesize.py` writes a deterministic
synthetic extract plus its own measured card, and the whole suite runs against that
offline.
