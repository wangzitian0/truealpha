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
| open, high, low, close | `market-price-fusion:v2` | 0.3% (30 bp) | USD |
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

## Ownership Boundaries

- #60 supplies source coverage, canonical-origin, knowability, and usage evidence.
- #207 supplies the versioned continuous confidence assessment.
- #343 supplies reconciliation and this row-complete report contract.
- #61 projects these reports into mart views, asset checks, trends, and the web page.
- Deployment and scheduling requests are expressed only through a released
  `infra2-sdk` contract; this report creates no infrastructure side effect.
