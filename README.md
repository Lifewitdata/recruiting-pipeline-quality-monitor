# Recruiting Pipeline Quality Monitor

A data-quality, anomaly-detection and alerting monitor over a **real** recruiting
funnel dataset, built in pure Python on DuckDB. 151 declarative checks, learned
thresholds, a cooldown-based alert router, and — the part that makes the numbers
credible — an incident-injection harness that measures how well the suite detects
faults it was never told about.

This project exists to answer a different question from a warehouse build. A
warehouse proves you can compute a number. This proves you can tell when a number
has quietly stopped meaning anything, say *which* rows caused it, and do that on a
timer without paging a human 600 times about one problem.

Every figure in this README was produced by running the code in this repo against
the exact bytes the source published. The commands to reproduce them are below.

```
Zenodo record 21390581  (JTH, CC BY-NC 4.0, not committed)
        │
        v
etl/fetch.py      sha256 + byte-size pinned per file
        │
        v
etl/load.py  ──>  raw.*   all-VARCHAR landing + _row_num + _loaded_at
             └─>  stg.*   typed views (select * replace try_cast(...))
                    └──>  stg.events  UNPIVOT of the ten funnel date columns
        │
        v
monitor/           151 checks in 6 families ──> dq.results
  engine ──> thresholds.py  learned bands (mean ± max(3·sd, floor))
         ──> alerts.py      fingerprint + dual cooldown  ──> dq.alerts
         ──> transports/    console · Slack webhook · SMTP (stdlib only)
        │
        v
monitor/incidents.py   inject 14 known faults, score detection ──> reports/
dashboard/             Streamlit triage console (5 pages)
.github/workflows/     ci.yml (offline, synthetic) · nightly-monitor.yml
```

## Quickstart

```bash
pip install -r requirements.txt

python etl/fetch.py            # downloads the 3 CSVs, verifies sha256 + byte size
python etl/load.py             # CSV -> raw.* -> stg.* views + _load_manifest
python monitor/cli.py run      # 151 checks -> dq.results, routes alerts
python monitor/cli.py summary  # recent runs at a glance
python monitor/cli.py drill format.zipcode_canonical.candidates   # the rows behind a finding

python -m monitor.incidents    # the detection scorecard (offline, synthetic fixture)

pip install -r requirements-dashboard.txt
cd dashboard && streamlit run streamlit_app.py
```

No source data in git. `data/`, `db/`, `tests/fixtures/` and `reports/` are
gitignored; `python etl/fetch.py --offline` re-verifies whatever is already on disk
without a network. If you would rather not download the licensed files at all, the
next section gives the three commands that run the whole suite against a generated
fixture instead — that is exactly what CI does.

Python 3.14.6, DuckDB 1.5.6 locally; CI runs Python 3.12 on the same DuckDB range.
`pytest -q` reports **67 passed**.

## What the monitor found in the real data

The latest run against the published JTH files judged **151 checks in 1,552 ms**:
**120 passed, 28 failed, 3 warned, 0 errored**. 141 of the run's result rows carry a
stored drill-down query — 139 distinct ones, every one of which executes against the
landed database, and `tests/test_dashboard.py` asserts exactly that rather than
trusting the SQL was valid when it was written.

The seven critical failures are the interesting ones, because four of them are the
publisher's own documentation disagreeing with the published files.

| Finding | Observed | Card claims | Why it matters |
|---|---|---|---|
| `contract.claim_missing.jobs.job_category` | 78.56 % null | 30 % | A coverage claim off by 48.6 points. Any "we have job categories for X % of vacancies" statement built on the card is wrong. |
| `contract.claim_missing.jobs.zipcode` | 2.13 % null | 68 % | The card's `zipcode` row is read correctly (candidates 41 %, jobs 68 %); the delivered file has 128 nulls in 6,011 rows. Off by 65.9 points in the other direction. |
| `contract.claim_missing.candidates.skills` | 11.37 % null | 5 % | Doubles the documented sparsity of the column a skill-matching model would train on. |
| `contract.claim_missing.jobs.skills` | 2.20 % null | 0 % | The card says the column is complete. It is not. |
| `trend.volume_last_month.candidates` | −98.89 % vs trailing median | — | The extract's final month is almost empty. A naive "this month vs last month" report reads that as a collapse in hiring. |
| `trend.volume_last_month.events` | −90.69 % vs trailing median | — | Same tail effect, independent metric. |
| `funnel.accepted_without_proposed` | 1 trajectory | 0 | A placement recorded as accepted with no offer ever proposed — a transition the funnel cannot make. One row, found and named. |

The 21 warning-severity failures are dominated by one defect class: French postal
codes that went through a numeric dtype. 21,949 populated candidate zipcodes are no
longer the documented two-digit departement code, 855 have lost a leading zero
outright, 32,186 `candidates.source` values arrived float-serialised (`6.0` where `6`
was meant) in a column the card types as required text, and 727 `(candidate_id,
job_id)` pairs appear twice even though the card states "each row = one candidate-job
pair". Each of those is a row-count, not an adjective.

The 18 published-rate contract checks are the most quotable part of the table
because of what *passes*: **12 of the 18 reconcile to within half a percentage
point**, including `jobs.daily_rate` (68 % claimed, 68.04 % actual) and
`candidates.job_category` (27 % claimed, 26.85 % actual). Two more sit just over the
band at +0.58 and +0.55 points and are reported as warnings — the card rounded, the
data agrees. The check is not tuned to produce findings; it produces them only where
the bytes disagree, which is four columns and nothing else.

Expectations are never hand-typed. The `contract` family parses the dataset card at
build time, so the checks test the document against the data rather than restating
it. The card is committed with a pinned sha256 (`f49d9e28…`) for the same reason: a
claim reconciliation is only reproducible if the claim text is frozen.

### And what ties

The `reconcile` family — 15 checks, **all passing on the real data** — is what makes
the findings above safe to publish. It computes every headline number twice, by two
independent paths, and fails when they disagree: the ten wide stage columns against
the unpivoted `stg.events` log, and the publisher's own label against the dates.

- `contract.event_count.history` and `reconcile.event_log_completeness` both land on
  **78,195 events** — the card's stated total, the sum of the ten dated columns, and
  the row count of the unpivoted log, three ways to the same number.
- `reconcile.label_partition`: the 42,288 trajectories partition exactly into
  `last_stage_reached` buckets, so the published label is a real partition and not an
  approximation.
- `reconcile.metric_placements`: **857 placements** whether you read the label or
  require `job_offer_accepted_date` to be populated.
- `reconcile.placement_rate_denominator_gap` records, as an `info` check that cannot
  fail, that the two defensible readings of "offer acceptance rate" are **93.3
  percentage points apart** — 857/899 of offers made versus 857/42,288 of all
  trajectories. One metric name, two honest denominators, and a number attached to
  the difference instead of a silent choice buried in a `select`.

A monitor that only ever reports failures cannot tell you whether its own arithmetic
is right. These checks exist to make the passing 120 mean something.

## The check registry

151 checks on the published card. The synthetic fixture registry holds 154: its
`history.csv` documents two columns the real file does not carry
(`application_id`, `application_source`), which generate three further checks — one
`contract.not_null` and two `format.text_type_fidelity`. Nothing is dropped on the
fixture and nothing is invented for it; the real registry is a strict subset.

| Family | Checks | What it defends |
|---|---|---|
| `contract` | 50 | Does the file agree with what the publisher *said* it contains — row counts, event count, column list, grain statement, per-attribute missing rates |
| `format` | 36 | Value formats and domains: float-serialised text, destroyed leading zeros, documented value sets the file does not use |
| `funnel` | 20 | Does the trajectory tell a consistent story — stage order, the published `last_stage_reached` label vs the dates, later dates populated over earlier ones |
| `trend` | 16 | Freshness, month-over-month volume, z-score anomalies, field adoption over time. Only meaningful on a timer |
| `reconcile` | 15 | The same number computed two independent ways: the ten wide stage columns against the unpivoted event log |
| `structure` | 14 | Key and referential structure: uniqueness, orphan foreign keys, join cardinality |

By kind: 81 `zero` (must be exactly zero), 26 `close` (within a band of a stated
value), 23 `drift` (learned from history), 17 `equal` (must tie), 4 `info` (recorded,
never fails). By severity: 76 critical, 66 warning, 9 info.

`structure` deliberately has no dbt equivalent here. On a third-party extract you
cannot add a constraint to the source — the only defence is to test the bytes on
every arrival and report what the grain actually is.

### Learned thresholds, not magic numbers

A drift check has no hardcoded limit. Each run records metrics into `dq.metrics`;
`monitor/thresholds.py` keeps a rolling mean and standard deviation in
`dq.baselines` and judges a value against

```
band = max(3 · sd, min_tol)      # needs MIN_HISTORY = 3 runs before it judges at all
```

Three properties make this usable rather than clever:

- **A floor, not just a sigma.** `min_tol` stops a byte-identical re-landing from
  paging on a third decimal place. On the live database all 23 baselines currently
  sit at their floor, because every recorded run landed the same published bytes and
  `sd = 0`. The dashboard says that out loud instead of presenting a fitted band
  that was fitted to nothing.
- **Seeding, not guessing.** Until a metric has 3 runs it is reported as *seeding*
  and cannot fail. A monitor that pages on its first run teaches people to ignore it.
- **One source of truth for the band.** The dashboard imports `SIGMA` and
  `MIN_HISTORY` from the engine rather than recomputing them, so the console can
  never display a tolerance the engine does not enforce.

Two anomaly detectors are paired on purpose, and the real data shows why. Across the
84 months carrying at least 100 events, the widest is May 2022 at 2,305 events —
**2.51 sigma** from the 929-event mean, under a 3-sigma bar. So
`trend.anomaly.monthly_events_zscore` passes and finds nothing, while
`trend.volume_last_month.events` fails on a 90.7 % collapse in the newest month. A
team that shipped the z-score check and stopped there would have a green monitor and
a missing month. Neither detector alone is a monitor.

## Alerting: one incident, not 600 alerts

`monitor/alerts.py` routes a **page** only for a critical failure or a check that
could not run at all. Everything else is recorded in `dq.results` and stays quiet.

- **Identity.** `fingerprint = sha1(check_id | scope)[:16]`. One check over one scope
  is one incident for as long as it stays open, however many runs it survives.
- **Dual cooldown.** An open incident re-pages only after `cooldown_runs = 4` runs
  *or* `cooldown_hours = 12.0` hours, whichever comes first. Both are read from
  `Policy`, and the dashboard imports `Policy` rather than retyping the numbers.
- **Verbs, not noise.** `NEW` opens, `STILL` is a cooldown expiry on an incident that
  never cleared, `BACK` is a re-occurrence after resolution, `CLEARED` closes.
- **Partial runs never close anything.** A `--family` run says nothing about the other
  families, so it must not resolve their incidents. An incident only clears on a run
  that actually tested it.

The live database demonstrates this directly: **7 incidents, all open, each seen on
3 runs and paged exactly once — 21 observations, 7 pages.** The third run routed
`0 alerts, 7 held by cooldown`.

Transports are `console`, a Slack incoming webhook and SMTP, all stdlib (`urllib`,
`smtplib`, `email.message`). No HTTP client library on the alerting path: an alert
router is the last thing you want broken by a dependency that failed to install the
night it mattered.

## Detection: the harness that scores the suite

A check suite that has never been shown a fault is a list of SQL queries someone
hoped were useful. `monitor/incidents.py` builds a synthetic fixture with the same
schema, warms the learned bands, injects **exactly one** known fault, re-runs the
whole registry, and scores what fired against what was *designed* to fire.

```
python -m monitor.incidents
```

Measured on the current code, 3 warm-up runs per incident, 154 checks in the registry:

| | |
|---|---|
| Incidents injected | **14** |
| Checks designed to fire | **26** |
| Checks that fired | **26** — 100.0 % detection |
| Incidents fully attributed to their designed checks | **14 / 14** |
| Incidents that needed a page and paged | **11 / 11** |
| Incidents that should have paged but stayed silent | **0** |
| Incidents that should have stayed quiet but paged | **0** |
| Control runs on the untouched fixture | **5** (2 judged) |
| False positives on clean data | **0 / 154 checks — 0.0 %** |
| Checks that changed status between control runs | **0** |

The 14 injected faults are the ones a real pipeline actually produces, split by
where they originate:

*Source file (13):* a truncated extract that loses 150 rows · an undocumented column
appearing · a documented column silently dropped · a code column passing through a
numeric dtype (`06` → `6.0`) · a published missing-rate drifting while the card still
says the old number · orphan foreign keys from a partial dimension load · a re-appended
delta colliding on the grain · a jitter that breaks stage ordering · an accepted
placement with no offer · rows dated before the collection window · a regional export
writing `DD/MM/YYYY` that casts to null · a new value in `last_stage_reached` · a whole
month missing behind one late surviving row.

*Warehouse model (1):* an incremental model change that deletes one stage from the
unpivoted event log while the wide table stays correct — the failure only the
`reconcile` family can see.

Two things make the score mean something. **Unattributed findings are reported, not
hidden:** a check that moved when the incident was not designed to move it is a
finding about the suite — a detector with a wider blast radius than its definition
claims — and the harness prints it. **The control run is what gives the detection
rate its meaning:** 100 % detection at a nonzero false-positive rate would be a
suite that pages on everything. Five clean runs, two judged after seeding, nothing
moving.

## Data Health console

Five Streamlit pages over the monitor's own tables — a triage console, not a BI
dashboard, and deliberately sharing no query layer with the reporting dashboard in
[Project 1](https://github.com/Lifewitdata/sales-pipeline-analytics-warehouse).

| Page | Question it answers |
|---|---|
| **Health** | Is this run trustworthy, and what changed since the last one? New / newly failing / recovered checks, per-family breakdown, cross-run trend, and the provenance card: which files, how many bytes, which sha256 |
| **Findings** | What exactly is wrong? Filterable, one click into the check's definition, the SQL that found the rows *executed on screen*, and that check's own history |
| **Thresholds** | Why did it decide that? Band as the engine computes it, mean ± band, and an explicit card for the metrics whose `sd` is 0 so a floor is not mistaken for a fit |
| **Alerts** | Who was interrupted, and is the cooldown holding? `occurrences` vs `notifications` per incident, findings vs pages per run |
| **Detection** | How good is this suite, actually? The incident report, per incident, with missed and unattributed checks named |

Verified in a real browser against the live 78,195-event database (all five pages)
and headlessly via `AppTest` in `tests/test_dashboard.py`, which renders each page
against a fixture database and asserts every stored drill-down executes.

## Running without the source data

`python etl/synthesize.py` writes schema-identical CSVs into
`tests/fixtures/synthetic/` — 2,000 candidates, 240 jobs, 3,000 trajectories. It
uses **no RNG at all**: pure index arithmetic plus a CRC32 for the missing-value
pattern, so the same bytes appear on every machine. The tests and the incident
harness land that fixture themselves, so:

```bash
python -m monitor.incidents     # the full suite, end to end, with no download
pytest -m "not incident"        # everything else
```

The fixture satisfies its own dataset card completely, which is what makes it usable
as ground truth: a fault injected into it has exactly one designed consequence, so a
check that fires without being designed to is a real finding about the suite rather
than noise about the data. `monitor/cli.py run` reads the landed `raw.*` tables and
so needs the real fetch; the two commands above do not.

Determinism is asserted, not assumed — CI synthesises twice and compares the output
byte for byte:

```bash
python etl/synthesize.py --out .gate/a
python etl/synthesize.py --out .gate/b
cmp .gate/a/history.csv .gate/b/history.csv
```

`.github/workflows/ci.yml` is fully offline: registry list → `pytest -m "not
incident"` → two syntheses compared byte-for-byte → the incident harness, whose
nonzero exit *is* the assertion → the console tests again, now that the harness has
written the report the Detection page reads → a detection-score table in the run
summary. Artifacts carry `dq.*` only.

That gate has already earned its place. It caught a dependency my local machine had
supplied transitively: `duckdb-python` needs `pytz` to materialise a `timestamptz`
column into a Python datetime, so every read of `dq.runs.started_at` or
`dq.alerts.first_seen` needs it. Seven tests passed locally and failed in CI on the
same commit. `requirements.txt` now names `pytz` and says why in its own comment,
rather than letting the project depend on whichever package happened to pull it in.

`.github/workflows/nightly-monitor.yml` is the scheduled monitor: 06:00 UTC,
`workflow_dispatch` with an optional refresh, a cached `data/` directory keyed on
`etl/fetch.py`, fetch → load → run, then export `dq.runs / results / metrics /
baselines / alerts` to CSV as a 30-day artifact. It fails on a missing run row or a
check that *errored* — not on a failing check, because a monitor that fails the
build when it finds a defect is a monitor that gets disabled. See
[`docs/SCHEDULED_RUNS.md`](docs/SCHEDULED_RUNS.md) for the full contract, including
why `--strict` is deliberately not used on the schedule and the `flock -n` line for
self-hosted cron.

## What is measured, what is not

Stated plainly, because a monitor's credibility is the sum of these claims:

- The **151 checks, 28 findings, 7 incidents and cooldown behaviour** are measured on
  the real published JTH files, downloaded and sha256-verified.
- The **14-incident / 26-check detection score** is measured on a synthetic fixture
  that carries the same schema. Real faults cannot be injected into a published
  third-party dataset.
- **Slack and SMTP adapters are tested against an in-process mock sink.** No message
  was delivered to a live workspace or mailbox. `transports.configure()` always
  attaches `console`, and adds Slack only when `SLACK_WEBHOOK_URL` is present and
  SMTP only when its `DQ_SMTP_*` variables are — so CI runs console-only with no
  configuration at all. What is proven is that a correctly-formed payload reaches a
  transport that records it, not that a notification landed in anyone's inbox.
- **No cloud warehouse is claimed.** DuckDB is the engine here. The Looker Studio
  spec in `docs/LOOKER_STUDIO_SPEC.md` is a build sheet for a BI tool, not evidence
  of a deployed report.
- The nightly workflow is written and reviewed but has **not** run on a schedule in
  this repository; the first cron dispatch is subject to GitHub's default-branch and
  60-day-idle rules, which `docs/SCHEDULED_RUNS.md` documents.

## Project layout

```
etl/        fetch.py (sha256-pinned) · load.py (raw.* -> stg.*) · synthesize.py
monitor/    card.py (parses the publisher's claims) · engine.py · thresholds.py
  checks/   base.py · structure · contract · format · funnel · trend · reconcile
  transports/  base · console · slack · email_smtp
            cli.py (run|list|summary|drill|alerts) · drilldown.py · alerts.py
            incidents.py (the injection harness) · db.py
dashboard/  streamlit_app.py · queries.py · app_pages/ (5 pages)
tests/      67 tests · mock_sink.py · helpers.py
docs/       SCHEDULED_RUNS.md · LOOKER_STUDIO_SPEC.md · upstream/dataset_card.md
.github/    ci.yml (offline) · nightly-monitor.yml
```

DuckDB state lives in `db/monitor.duckdb` (`MONITOR_DUCKDB` overrides). The five
`dq.*` tables are the whole contract between the engine and the console:
`runs`, `results`, `metrics`, `baselines`, `alerts`.

## Data source and licence

**JTH (Job Tracking History) v1.0, July 2026** — 37,554 candidates, 6,011
vacancies, 42,288 candidate–job trajectories, 78,195 funnel events, collected by
professional recruiters in France from March 2018 to April 2025. The funnel is
published as ten dated columns per trajectory; `stg.events` unpivots them, and the
`reconcile` family proves the sum of those ten columns is exactly the 78,195 events
the publisher states.

The publisher's own citation, copied verbatim from the dataset card:

```bibtex
@inproceedings{romero2026jth,
  title = {JTH: A Dataset for Evaluating Cold-Start and Temporal Dynamics in Job Recommendation},
  author = {Romero, Julien and Millet, Yann and Behar, Eric},
  booktitle = {Proceedings of the 20th ACM Conference on Recommender Systems (RecSys '26)},
  year = {2026},
  publisher = {ACM},
  doi = {10.1145/3773078.3831846}
}
```

`etl/fetch.py` downloads the three CSVs from **Zenodo record
[21390581](https://zenodo.org/records/21390581)**, which is the record that served
the exact bytes whose sha256s are pinned in that file. The card's own Resources
section links record 20020466; the fetcher's record is the one verified against the
pinned hashes, and `python etl/fetch.py --offline` re-checks all four files (three
CSVs plus the card) without touching the network.

**CC BY-NC 4.0** — non-commercial research use; the card states that commercial use
and de-anonymisation are "strictly forbidden". The CSVs hold pseudonymised records of
real job candidates: deterministic digest identifiers, raw free text removed, salary
and daily-rate fields µ-aggregated, Laplace-noised and clipped at the 95th
percentile, dates jittered with a 5-day Laplace scale under monotonic sorting, and
k = 5 anonymity over `{zipcode, experience bucket, contract type}`. So **the data
files are not committed to this repository** — only the checksum-pinned fetcher is.
`docs/upstream/dataset_card.md` is reproduced because the `contract` family reads its
claims at build time, and it is pinned by sha256 for the same reason; it is
attribution and documentation, not a re-distribution of the data.

## Licence

The code in this repository is MIT. The upstream dataset is CC BY-NC 4.0 and this
licence does not change that; nothing here grants the right to re-distribute the
JTH data files.
