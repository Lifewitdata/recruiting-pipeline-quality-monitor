# Looker Studio report spec

`dashboard/` is the executable version of this document: the Streamlit console reads
the monitor's own `dq.*` tables and is what you should use to triage a bad night,
because only it can run a stored drill-down query. Looker Studio cannot be built from
code, so this file is the build sheet for the *standing* report - the one an
operations lead or a hiring manager keeps open to answer "is the funnel data
trustworthy today, and has that changed?" without asking anyone.

Same shape as Project 1's spec: one section per page, each naming the exact table,
field, aggregation and filter. No page here needs a calculated field for a metric -
the monitor already computed it and wrote it down.

## Which tables, and which of them may be published

| table | grain | one row is |
|---|---|---|
| `dq.runs` | one monitor invocation | what a run judged and what it concluded (`checks_total`, `passed`, `warned`, `failed`, `errored`, `alerts_sent`, `wall_ms`, `snapshot_sha`) |
| `dq.results` | one check, one run | the verdict, with `observed`, `expected`, `tolerance`, `unit`, `detail`, `severity`, `status`, and the `drilldown_sql` that finds the rows |
| `dq.metrics` | one metric, one run | the raw number a drift check later learns its band from (`metric_key`, `bucket`, `value`, `recorded_at`) |
| `dq.baselines` | one metric | what the monitor has learned: `mean`, `sd`, `n_runs`, `min_tol`, `last_value`, `updated_at` |
| `dq.alerts` | one incident (`fingerprint`) | paging state: `status`, `first_seen`, `last_seen`, `occurrences`, `notifications`, `runs_since_notify`, `last_notified_at`, `subject`, `body` |

`dq.*` is the monitor's own state - it describes the data, not a person, and it is the
only thing from a production run that may leave the runner. `raw.*` and `stg.*` hold
JTH rows (third-party personal data, CC BY-NC 4.0) and must never be pointed at a BI
tool. Every field named below comes from one of the five tables above.

## Data source setup (once)

Pick one. Both are honest; the first is what this repo's CI already produces.

| option | connector | how the tables get there | suited to |
|---|---|---|---|
| A - BigQuery | BigQuery | the nightly workflow uploads `out/dq_*.csv` (`actions/upload-artifact`); a load step or `bq load` puts them in a `monitoring` dataset | you already run Project 1 in BigQuery and want one place to look |
| B - CSV upload | File upload | download the five `dq_*.csv` from the latest workflow run and upload them as a Studio data source, or point Studio at a folder with Google Sheets auto-refresh | a standing report with zero infrastructure; refresh is manual, which is fine for a weekly read |

Field roles to set on connect (Studio auto-detects most):

- `started_at`, `finished_at`, `recorded_at`, `updated_at`, `first_seen`,
  `last_seen`, `last_notified_at` → Date, day precision; keep a second copy of
  `started_at` as a Date/DateTime dimension for ordering runs within a day.
- `check_id`, `family`, `scope`, `severity`, `status`, `metric_key`, `bucket`, `unit`,
  `fingerprint` → Text. Set `status` and `severity` as dimensions you filter on, never
  measures.
- `checks_total`, `passed`, `warned`, `failed`, `errored`, `alerts_sent`, `wall_ms`,
  `n_runs`, `occurrences`, `notifications`, `runs_since_notify` → Number, aggregate
  SUM (last-run values, so aggregate over the rows you have already filtered to one run).
- `observed`, `expected`, `tolerance`, `mean`, `sd`, `min_tol`, `band`, `last_value`,
  `value` → Number. **Never aggregate these with AVG across checks** - they are not
  commensurable; a null rate in percent and a row count in the tens of thousands would
  average into nonsense. Aggregate only within one `check_id` or one `metric_key`.
- `detail`, `subject`, `body`, `drilldown_sql`, `snapshot_sha` → Text. Set
  `drilldown_sql` to hidden on every page: it is a query string, not a metric, and the
  Streamlit console is where it gets executed.

Calculated fields, exactly two:

| name | formula | why |
|---|---|---|
| `pass rate` | `passed / checks_total` | the one health number worth reading off a scorecard; do not compute it as a proportion of `SUM(passed)` across all runs, which mixes runs of different sizes |
| `band width` | `IFNULL(sd, 0) * 3` then take the greater of that and `min_tol` | `dq.baselines` stores the inputs, not the band; this is the same expression the engine judges with. If Studio cannot do a `GREATEST`, add `CASE WHEN sd*3 > min_tol THEN sd*3 ELSE min_tol END` |

Filters: put `started_at` (or `run_id`) as a report-level date-range control.
`family` belongs on the check-level pages only; `dq.runs` has no family column, so a
report-wide family filter would silently empty the overview page.

## Page 1 - Health scorecard

Answers "can we trust this morning's numbers" in one screen. Source data must be
scoped to a single run: add a filter `started_at = MAX(started_at)` (or bind a
dropdown to `run_id` and default it to the newest).

| element | type | source | fields |
|---|---|---|---|
| Checks judged | Scorecard | `dq.runs` | `checks_total` |
| Findings | Scorecard | `dq.runs` | `failed + errored`, conditional red at > 0 |
| Critical findings | Scorecard | `dq.results` filtered `status = 'fail'` and `severity = 'critical'` | `check_id` (COUNT) |
| Open incidents | Scorecard | `dq.alerts` filtered `status = 'open'` | `fingerprint` (COUNT) |
| Alerts sent | Scorecard | `dq.runs` | `alerts_sent` |
| Run took | Scorecard | `dq.runs` | `wall_ms` |
| Pass rate | Scorecard | `dq.runs` | calculated `pass rate`, as % |
| Findings over runs | Time series | `dq.runs` | date `started_at`, measures `failed`, `errored`, `warned` as three lines |
| Findings by family | Bar (horizontal) | `dq.results` | dimension `family`, measure `check_id` (COUNT), filter `status in ('fail','error')` |
| Last run detail | Table | `dq.runs` limited to 1 | all columns except `finished_at` |

Caption to include verbatim: "A run that judged fewer checks than the fullest run on
record is incomplete, not clean. Unmeasured checks are never counted as passes."

## Page 2 - Findings and root cause

One row per check for the selected run. Source: `dq.results`, filtered to one
`run_id` and to `status in ('fail','error','warn')` by default, with a control to show
passing checks.

| element | type | fields |
|---|---|---|
| Findings table | Table, sortable, 50 rows | `check_id`, `family`, `scope`, `severity`, `status`, `observed`, `expected`, `unit`, `detail` |
| Severity distribution | Donut | dimension `severity`, measure `check_id` (COUNT) |
| Worst offenders | Bar | dimension `check_id`, measure `observed`, filter `severity = 'critical'`, sort desc |
| What a finding means | Text, bound to a table selection | `name` and `description` are stored verbatim on `dq.results`, so this page explains the check without a second copy of the definition |

Sort the table by the same rule the router uses - fail before error before warn, then
critical before warning - so the top of the page and the page-out agree.

`drilldown_sql` is stored but not runnable in Studio. Put a note on the page:
"the SQL that identifies the rows behind any finding is stored with the finding;
open the Streamlit console's Findings page to execute it."

## Page 3 - Thresholds the monitor learned

Source: `dq.baselines` for the band, `dq.metrics` for the history.

| element | type | source | fields |
|---|---|---|---|
| Metrics tracked | Scorecard | `dq.baselines` | `metric_key` (COUNT distinct) |
| Still seeding | Scorecard | `dq.baselines` filtered `n_runs < 3` | `metric_key` (COUNT) |
| Band floor cases | Scorecard | `dq.baselines` filtered `sd = 0` | `metric_key` (COUNT) |
| Baseline table | Table | `dq.baselines` | `metric_key`, `n_runs`, `mean`, `sd`, `min_tol`, calculated `band width`, `last_value` |
| Metric history | Time series | `dq.metrics` for one `metric_key` | date `recorded_at`, measure `value`, plus a reference line at `mean` and bands at `mean ± band width` |
| Metric distribution | Histogram | `dq.metrics` | dimension `bucket`, measure `value` |

Caption to include verbatim: "Thresholds are learned from this monitor's own run
history, floored at each check's minimum tolerance. A metric that has never moved has
a zero standard deviation, so its visible band is the floor - not a fitted range."

This page is the argument for the whole project: it shows a manager that the alert
line was derived from data rather than typed into a config file by whoever was on call.

## Page 4 - Incidents and paging

Source: `dq.alerts`. This page exists to prove the cooldown works, which is the
difference between an alerting system people use and one people mute.

| element | type | fields |
|---|---|---|
| Open incidents | Scorecard | `fingerprint` (COUNT), filter `status = 'open'` |
| Seen vs paged | Combo chart | dimension `check_id`, measures `occurrences` (bar) and `notifications` (line) |
| Incident table | Table | `check_id`, `scope`, `severity`, `status`, `first_seen`, `last_seen`, `occurrences`, `notifications`, `runs_since_notify`, `subject` |
| Longest open | Bar | dimension `check_id`, measure `last_seen - first_seen`, filter `status = 'open'`, sort desc |
| Alert text | Table, expanded row | `subject`, `body` |

Read `occurrences` against `notifications` on the combo chart: an incident seen twelve
times that paged once is the cooldown working. Equal bars and lines on a long-running
incident mean routing is broken, not that the data is worse each time.

## Page 5 - Detection, measured off the incident harness

This page is not built from `dq.*`. It reads the JSON the incident harness writes
(`reports/incident_report.json`), uploaded as a workflow artifact and loaded as a
single-row table plus one table of per-incident rows.

| element | type | fields |
|---|---|---|
| Detection rate | Big number | `detection_rate_pct` |
| Incidents injected | Scorecard | `incidents` |
| Checks that fired | Scorecard | `checks_detected` of `checks_expected` |
| Silent criticals | Scorecard, red at anything above 0 | length of `incidents_that_should_page_but_did_not` |
| Over-eager paging | Scorecard | length of `incidents_that_should_not_page_but_did` |
| Control false positives | Scorecard | `control.false_positive_rate`, with `control.runs` as the denominator in the caption |
| Per-incident breakdown | Table | `id`, `layer`, `story`, count of `expected`, count of `detected`, count of `missed`, `detected_share`, `should_page`, `paged`, count of `unattributed` |
| Missed checks | Table, filtered `missed` non-empty | `id` and the missed check ids |

Caption to include verbatim: "Detection is measured by injecting faults and scoring
which named checks fired. A suite nobody has tested this way reports a coverage claim,
not a detection rate."

## Layout and style

- Match Project 1's palette so both reports read as one person's work: primary
  `#0969da`, red `#cf222e`, amber `#bf8700`, green `#1a7f37`, borders `#d0d7de`,
  light background.
- Status colours are semantic here, not decorative: bind `status` to a colour mapping
  (fail/error red, warn amber, pass green) once at the data-source level so the same
  verdict is the same colour on all five pages.
- Header on every page: the `run_id` in scope and its `snapshot_sha`, truncated to
  sixteen characters. A data-health report without provenance is just another chart.
- No sparkline in a scorecard on page 1 for `failed`: the trend belongs on the time
  series, and two versions of the same number invite someone to read the wrong one.

## Acceptance check

Build the report, then run the incident harness and load its output. The report must:

1. Show the latest `dq.runs` row on page 1 with a non-zero `checks_total` and the
   findings count matching `dq.results` for that `run_id`.
2. On a run where a critical incident was injected, show it on page 2 *and* an open
   row for it on page 4 - a finding that never reaches the incident table is a finding
   nobody was told about.
3. On page 4, show `occurrences` greater than `notifications` for an incident observed
   across more runs than the cooldown allows - that single chart is the proof the
   router is not spamming.
4. On page 3, show at least one metric with `n_runs >= 3` and a non-zero band, and say
   which metrics are still seeding.
5. On page 5, reproduce the harness's own numbers exactly. If the report and the
   harness disagree, the report is wrong: it is reading a copy of the JSON, and the
   harness printed the truth.
