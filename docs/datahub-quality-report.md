# DataHub Multi-Source Quality Report

This contract turns one exact service demand into a reproducible acquisition-quality
report. It is the machine-readable input to #61's mart/dashboard work, not a dashboard
or a Production SLO gate.

## Reconciliation

For each requested semantic cell, DataHub performs these steps in order:

1. Exclude assertions whose source-derived `knowable_at` is after the report cutoff.
2. Exclude sources absent from the versioned reconciliation policy, while retaining
   their assertion IDs and a reason code in the result.
3. Select one representative per canonical origin group. A mirror or reseller of the
   same original source does not create independent evidence.
4. Select the primary representative by the semantic cell's declared, content-addressed
   `source_priority` policy. Different fields may bind different policies; confidence
   and ingestion order never arbitrate truth.
5. Compare numeric origin representatives using Decimal arithmetic:

   `abs(a - b) <= absolute_tolerance + relative_tolerance * max(abs(a), abs(b))`

   Non-numeric normalized values use exact canonical-value SHA-256 equality. The report
   retains the hash, not the normalized or raw payload bytes.
6. Retain agreement, insufficient independent origins, conflict/abstention,
   not-yet-knowable, and unavailable as distinct outcomes.

The selected assertion carries its existing normalized continuous confidence in
`[0, 1]`. Presentation layers may multiply it by 100 for display, but this module
does not recalculate or override the confidence policy owned by #207.
Factor projection remains provenance-neutral; source and origin metadata stay in the
runner/report boundary.

### Every field of the price bar is its own cell

A market-price observation asserts the session's whole bar — `open`, `high`, `low`,
`close`, `volume` — from both origins (parser `production-topt-live-parser:v10`,
`twelve-data-parser:v3`). The report reconciles each field as its own
`ReconciliationCell` (its own `field_semantics_id`, unit and policy), so "several
metrics at HIGH confidence" is a count of fields whose two origins agreed, not a count
of cells whose close did:

| field | policy | relative tolerance | unit |
|---|---|---|---|
| open, high, low, close | `market-price-fusion:v3` | 0.3% (30 bp) | USD |
| volume | `market-volume-fusion:v1` | 2% (200 bp) | shares |

Volume is not a price: it is each vendor's own aggregation of the consolidated tape and
settles later than the prices (late prints, corrections). The settled 2026-08-14 AAPL
bars on the cassette pair agree exactly on volume, so the 2% exists for the
same-evening capture; a primary-listing-only count (roughly half the tape) still
conflicts by an order of magnitude. Like the price tolerance, it moves from measured
spread in a version, never from a guess.

An origin contributes an assertion to a field only when its payload carries a value
for it. A Twelve Data bar refused because a post-close print had moved it corroborates
`close` alone, so its other four fields grade `insufficient_independent_origins` for
that listing; a field no origin asserted (observations written before v10) grades
`unavailable`. Two nulls never agree.

In the persisted payload each `reconciliation_cells[listing]` keeps the close's grade
under the headline keys (`outcome`, `origin_groups`, `selected_source`,
`selected_value`, `conflicting` — what the pointer gate and the admin page read) and
carries every field's grade under `fields[<field>]` with its `policy_id`;
`field_reconciliation[<field>]` summarises `agreed` / `cells` / `share` per field over
the graded market-price cells. The headline `independent_reconciliation` ratio stays
the close's, over the full requested denominator.

### Policies in force

- `market-price-fusion:v3` (`quality_report.RECONCILIATION_POLICY`): priority
  `yahoo-chart:v1`, `twelve-data:v1`, `moomoo-kline:v1`; relative tolerance 0.3%;
  assertions narrowed to the served bar's trading day first (#622).
- `financial-fact-fusion:v1` (`quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY`):
  priority `sec-company-facts:v1`, `moomoo-financials:v1`; relative tolerance 1%;
  reconciled per field (`revenue`, `gross_profit`, `net_income`, `total_assets`) at the
  PRIMARY's fiscal period end. The report carries `financial_fact_reconciliation_cells`
  per subject with the per-field outcomes, and its own KPI pair
  `financial_fact_independently_reconciled_count` /
  `financial_fact_independent_reconciliation` (agreed subjects over the subjects with a
  primary); the headline `independent_reconciliation` stays the close's. A subject counts
  as independently reconciled only when every compared field agreed, and any conflicting
  field abstains it. A second origin that has not published the primary's period, or
  reporting in another currency, is absent for that field
  (`insufficient_independent_origins`), never a conflict. See
  `docs/price-source-calibration.md` for the measured tolerance.

### Primary failover (#862)

Yahoo has no SLA (init.md §5, §9). When it cannot serve a market-price cell —
`SourceUnavailableError` (`transient_network`) or `TimeoutError` (`timeout`) after every
retry, or no bar at all (`field_unavailable`) — the capture executor asks the source for a
failover, and the market-price adapter asks the registered origins in
`market-price-fusion:v3` priority order (`twelve-data:v1`, then `moomoo-kline:v1`) for the
close of the target's settled session. The first that has it serves the cell:

- as itself: its parser vintage, its bytes under its own vendor prefix, its record id
  (`<origin>:<symbol>:<session>`), its vintage under the cell's planned request — so the
  terminal attempt names it and the snapshot binds it; the mart serves that origin's number;
- declared: the payload carries `served_by_failover: "<origin>"`, and the obligation result
  carries `served_by_failover` beside the primary's reason codes (the attempts keep the
  primary's failure; the terminal attempt is `success` with the primary's reason);
- one grade lower: the origin's normal confidence minus the 0.10 step a session of lag
  costs (Twelve Data 0.85 -> 0.75, moomoo 0.80 -> 0.70), floored at 0.50;
- only for the same session, knowable by the run's cutoff instant: another day's close, or
  one knowable after the cutoff, cannot serve. Without a market calendar a holiday tick's
  target session has no bar at any origin, so a primary failure on a holiday still leaves
  the cell unavailable.

The remaining origins still corroborate. The report grades the cell on what asserted it:
`selected_source` is the serving origin, `agreed` only when a second independent origin
asserted the same session (then the pointer gate counts it), otherwise
`insufficient_independent_origins` — never agreed on one origin. The cell carries
`served_by_failover` beside its headline keys and `served_by_failover_count` totals the
run; the tick's summary line says `served by failover N`. When no origin can serve, the
cell resolves exactly as before the failover existed. A failover-served observation never
anchors cross-run reuse (#635 requires the primary's parser vintage), so the next tick asks
the primary again. The `fusion-selects-by-priority-not-recency` output invariant accepts a
non-primary selection only when the payload declares the failover, the policy ranks the
origin, and no higher-ranked origin asserted the same session on that obligation.

## Fixed Denominator

`VersionedDataHubQualityReport.cells` contains exactly one row per requested cell,
including unplanned, pending, failed, unavailable, unchanged, stale, and conflicted
cells. A content-addressed `DataHubQualityDenominator` binds the accepted service-demand
ID to the complete requested-cell ID set; report rows must match it exactly. Consumers
pin that denominator ID, so omitting a failed cell changes the identity and fails the
expected-demand comparison. Every row binds its field-level reconciliation policy, and
the report bundles the exact content-addressed policies needed to validate thresholds.
All summary ratios use this requested-cell count as their denominator:

- `planned_coverage = planned_count / requested_count`
- `terminal_coverage = terminal_count / requested_count`
- `availability = selected_assertion_count / requested_count`
- `freshness = fresh_selected_count / requested_count`
- `independent_reconciliation = agreed_cell_count / requested_count`
- `lineage_completeness = complete_lineage_cell_count / requested_count`
- `denominator_mean_confidence_score = sum(selected confidence; otherwise 0) / requested_count`

This prevents a failing collector from improving its own metrics by omitting failed or
missing cells. Reconciliation results also retain the policy threshold and explicit
comparison anchor, so an apparent agreement or conflict can be validated without trusting
producer-supplied outcome labels. `origin_composition` counts each origin at most once per
requested cell.

## Representative Report

The following compact projection shows the intended operator view for four requested
cells. The actual contract also contains content-addressed cell/result IDs, exact
lineage coordinates, retry counts, unchanged-response counts, and reason codes.

```json
{
  "requested_count": 4,
  "planned_count": 3,
  "terminal_count": 3,
  "available_count": 1,
  "fresh_count": 1,
  "independently_reconciled_count": 1,
  "conflicted_count": 1,
  "complete_lineage_count": 2,
  "planned_coverage": "0.75",
  "terminal_coverage": "0.75",
  "availability": "0.25",
  "freshness": "0.25",
  "independent_reconciliation": "0.25",
  "lineage_completeness": "0.5",
  "denominator_mean_confidence_score": "0.1875",
  "origin_composition": [
    {"origin_group_id": "origin:sec:v1", "cell_count": 2},
    {"origin_group_id": "origin:vendor-a:v1", "cell_count": 2}
  ]
}
```

The low aggregate score is intentional: one independently reconciled value at `0.75`,
one conflict, one failed acquisition, and one unplanned cell produce
`0.75 / 4 = 0.1875`. A presentation layer may display that as 18.75/100. The report
exposes the service gap rather than averaging only successful rows.

## Manual re-run with a forced fetch

A datahub tick runs on its schedule and can also be launched by hand (#874). A
scheduled tick never forces a fetch. It satisfies an obligation from observations that
another run committed in the last twelve hours, when they have the same subject,
semantic, parser vintage and identity coordinates (#635). That reuse is what keeps the
night's TOPT, QQQ and canary ticks inside the vendor budget. As a side effect, a
same-day re-run reuses the night's observations and never calls a vendor.

A manual launch with `force_fetch: true` skips the reuse window for every obligation:

- **Every obligation is fetched once.** Identity rules are unchanged. Bytes the vendor
  already sent collapse onto the existing `raw.fetches` row and object. Changed bytes
  land as a new vintage, and the forced run serves that vintage. The run goes through
  the same freeze, the same report and the same a1 pointer gate as a scheduled tick.
  Later ticks inside the window reuse the forced capture. If it ties on completion time
  with the unforced run of the same `executed_at`, the forced capture is chosen.
- **The run has its own identity.** Its capture version is the tick's version with a
  `-forced` suffix (`live-20260916T2130-forced`). The unforced run of the same
  `executed_at` is settled history: resumed when it completed, refused when it
  degraded (#538). The suffix is how a forced launch fetches again at that same
  timestamp instead of landing on that run.
- **A retry is idempotent.** Retrying the same launch, with the same `executed_at` and
  `force_fetch`, resolves to the same forced run. A complete run resumes without a
  vendor call; a degraded one is refused with the reason already on file. To fetch
  again, launch with a new `executed_at`.
- **The forcing is recorded.** It appears in the run plan, in the quality report, in
  the op's output metadata (`forced_fetch`) and as `(forced fetch)` in the tick's log
  line. A forced re-run therefore cannot be mistaken for scheduled-tick evidence. Both
  database records are JSONB `payload` columns:

  ```sql
  select plan.run_id, (plan.payload->>'forced_fetch')::boolean as forced_fetch
  from raw.production_topt_run_plans plan
  order by plan.created_at desc limit 5;

  select report.run_id, (report.payload->>'forced_fetch')::boolean as forced_fetch
  from mart.datahub_quality_report report
  order by report.created_at desc limit 5;
  ```
- **Mind the vendor clock.** The fetch happens when the run executes, whatever
  `executed_at` says. Between about 23:00 and 07:00 America/New_York, Yahoo's overnight
  rebuild nulls the latest close (#622). Until about 16:30 the session's bar is still
  moving.

There are two ways to launch a forced run:

1. **The admin page.** On `/admin`, tick "Force a fresh vendor fetch" and press
   "Trigger a run now". This inserts a `staging.pipeline_trigger_requests` row with
   `force_fetch = true`. `pipeline_trigger_sensor` launches `topt_live_pipeline` with
   that config within about 30 seconds.
2. **Dagster GraphQL.** Send `launchRun` to the webserver's `/graphql` endpoint:

   ```graphql
   mutation LaunchForcedTick($executionParams: ExecutionParams!) {
     launchRun(executionParams: $executionParams) {
       __typename
       ... on LaunchRunSuccess { run { runId } }
       ... on RunConfigValidationInvalid { errors { message } }
       ... on PythonError { message }
     }
   }
   ```

   Use these variables. Set `executed_at` to the cutoff you want (ISO 8601 with an offset):

   ```json
   {
     "executionParams": {
       "selector": {
         "repositoryLocationName": "data_engine.dagster_defs",
         "repositoryName": "__repository__",
         "jobName": "topt_live_pipeline"
       },
       "runConfigData": {
         "ops": {
           "run_topt_live_tick": {
             "config": {
               "executed_at": "2026-09-16T21:30:00+00:00",
               "force_fetch": true
             }
           }
         }
       }
     }
   }
   ```

   `repositoryLocationName` is the code location's name as the webserver lists it; CI
   starts the code server with `--location-name data_engine.dagster_defs`. To confirm it,
   run `{ repositoriesOrError { ... on RepositoryConnection { nodes { name location { name } } } } }`.
   `qqq_live_pipeline` (op `run_qqq_live_tick`) and `canary_live_pipeline` (op
   `run_canary_live_tick`) accept the same config.
   `apps/data-engine/tests/test_dagster_defs.py` validates the payload above against the
   deployed job's config schema.

## Ownership Boundaries

- #60 supplies source coverage, canonical-origin, knowability, and usage evidence.
- #207 supplies the versioned continuous confidence assessment.
- #343 supplies reconciliation and this row-complete report contract.
- #61 projects these reports into mart views, asset checks, trends, and the web page.
- Deployment and scheduling requests are expressed only through a released
  `infra2-sdk` contract; this report creates no infrastructure side effect.
