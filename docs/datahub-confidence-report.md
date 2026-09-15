# DataHub Confidence & Accuracy Report

The owner's standard (2026-09-15): *"the report is accurate; HIGH confidence = multiple
sources agree, MEDIUM = multiple sources present, LOW = covered by one source; several
metrics at each level; ≥3 sources connected."*

`apps/data-engine/src/data_engine/datahub/confidence_report.py` grades every
(metric family, subject) cell of the run a governed pointer heads, aggregates per family and
per universe, samples ten subjects across every origin, runs the accuracy oracle, and
appends one content-addressed row to `mart.datahub_confidence_report`. The Dagster job
`datahub_confidence_report` (`lanes/quality.py`) runs it nightly at 00:45 UTC — after
`output_invariants_check` (00:15) — once per lane universe (`universe-list:qqq`, `topt`),
and can be launched by name with an explicit `executed_at`. `/admin/datahub` renders the
newest report per universe.

## Bands

| band | rule |
|---|---|
| `high` | at least two **independent** origins asserted a value and the family's declared, content-addressed reconciliation policy graded them `agreed` (`reconcile_source_assertions`, #343) |
| `medium` | at least two origins asserted a value, but: no agreement policy exists for the family (`no_agreement_policy`), or they disagree beyond tolerance (`not_agreed_within_tolerance`), or they share one lineage (`same_lineage`) |
| `low` | exactly one origin asserted a value (`single_origin`; `second_origin_other_day` when a session-bound second origin published a different day, #622) |
| `missing` | no origin asserted a value |

**Independence is lineage, not origin id.** A mirror, a reseller, or a second parser of the
same document never corroborates its original (docs/datahub-quality-report.md, step 3). The
headcount plane's two producers (`10k-extraction`, `manual-review`) both read the issuer's
10-K and are one lineage: equal values grade `medium`, never `high`.

**The stored `confidence` column is not used.** `staging.capture_normalized_observations.confidence`
is stamped per semantic by the parser (0.85 market-price, 0.92 financial-fact, 1.00
identity on the current heads) — not the formula in docs/confidence-calibration.md. The
report measures the distinct values per semantic in the run and records
`metadata.stored_confidence.used_for_bands = false`.

## Families and policies

| family | semantic | origins today | policy |
|---|---|---|---|
| `close` | market-price | `origin:yahoo:v1`, `origin:twelve-data:v1` | `market-price-fusion:v2` (`quality_report.RECONCILIATION_POLICY`, 30 bp relative), served-day narrowing |
| `revenue`, `gross_profit`, `pre_provision_profit`, `total_assets`, `shares_outstanding`, `net_income` | financial-fact | `origin:sec-company-facts:v1` | none — single origin, `low` |
| `headcount` | financial-fact | `origin:headcount:<producer>` per producer in `staging.issuer_headcount_facts` | none — one lineage, `medium` at most |
| `index_membership` | index-membership plane (QQQ) | `origin:nasdaq-index:v1` (`staging.etf_constituent_facts`), `origin:nport:v1` (`mart.fund_holdings_resolved`) | `index-membership-fusion:v1` (new), presence compared exactly |
| `etf_weight` | index-membership plane (QQQ) | `origin:nport:v1` today; `origin:nasdaq-index:v1` once the operator route carries a weight | `index-membership-fusion:v1`, weights compared at the stated tolerance |

`close` is exactly what the pointer gate calls corroborated: the report re-runs the same
engine over the same observations and records whether its per-listing outcomes match the
persisted `mart.datahub_quality_report` row for the run
(`accuracy.close.matches_quality_report`).

### Index membership policy (new)

`INDEX_MEMBERSHIP_POLICY`: the index operator's constituent list is the pinned primary
(the governed universe is published from it, #539); the fund's own N-PORT holdings are the
independent second route (#63). Both routes are resolved at the head's cutoff: the newest
constituent refresh knowable by then, and the newest N-PORT vintage filed by then.

- **Membership** (`index_membership`) is compared exactly: both routes list the listing →
  `agreed`; one route only → `low` (a name added since the last quarterly filing, or a
  line held but dropped from the index, is a real single-origin cell — the denominator is
  the union of both routes).
- **Weights** (`etf_weight`, same policy) are compared at absolute 0.05 percentage points
  + 10 % relative. A quarter-end filed weight and an operator weight taken weeks later
  differ by price drift (a few percent); the tolerance exists to catch a mis-joined line
  (an order of magnitude), not drift. Today `staging.etf_constituent_facts.weight` is NULL
  by design, so the weight has one origin (N-PORT) and grades `low`; membership itself
  is still corroborated by both routes.
- N-PORT lines that resolve to no listing (`holdings_unresolved_to_listing`) are counted in
  the family's `routes` notes rather than silently dropped.

## Report shape

```
report_version, universe, universe_id, run_id, cutoff, generated_at, environment
bands, independence_rule, subjects, sources_connected
families.<family>: cells, high, medium, low, missing, share, compared, agreement_rate,
                   tolerance, tolerance_policy, origins, reasons [, routes]
cells.<family>.<subject>: band, reason, origins, independent_origins, outcome, delta,
                          relative_delta, comparison, excluded
sample.<subject>.<family>: values{origin: value}, delta, relative_delta, verdict, reason
accuracy.close: origins, compared, agreed, agreement_rate, tolerance_policy,
                quality_report_id, matches_quality_report
accuracy.sec_oracle: fields, issuers_requested, issuers_compared, rows[], per_field, skipped
metadata.stored_confidence: used_for_bands=false, values_by_semantic, constant_per_semantic
```

`agreement_rate` is `high / compared`, where `compared` counts only cells the engine
actually compared (two independent origins under a policy). Shares use the family's cell
count as denominator; a `missing` cell stays in it.

The default sample is five QQQ names (AAPL, MSFT, NVDA, AMZN, GOOGL) and five TOPT issuers
spanning every operating branch (JPM, BRK.B, XOM, V, COST); the job's `sample_subjects`
config overrides it.

## Accuracy

- **close**: the yahoo ↔ twelve-data agreement the engine computed, with the policy and
  the cross-check against the persisted quality report.
- **fundamentals**: `quality.vendor_oracle`'s deliberately independent SEC re-derivation
  (wider concept lists, latest period across variants — it does not import the adapter)
  for a sample of issuers (default 5: the sample subjects first, then the universe in
  order), fetched live through the source gateway. Agreement is reported per field
  (`revenue`, `gross_profit`) over issuers where the vendor asserts a value, with the
  period mart's figure came from and its staleness in years. Without an SEC user agent the
  section says `no_sec_user_agent` and compares nothing.

## What it shows today, honestly

- `high`: `close` (QQQ 2026-09-14 head: 101/102 agreed; TOPT: 21/21) and
  `index_membership` (QQQ: 101 of 102 constituents are also in the 2026-06-30 N-PORT
  vintage; the one constituent-only name and the one unresolved N-PORT line grade `low`).
- `medium`: `headcount` where both 10-K producers wrote (one lineage; 20 TOPT issuers on
  the production plane).
- `low`: every other fundamental — one origin (SEC company-facts) — and `etf_weight`
  (N-PORT only; the operator route carries no weight).
- `missing`: cells no origin filled (e.g. `pre_provision_profit` outside the financial
  branch, headcount for issuers the plane has not covered).

What raises fundamentals to `high`: a second, independent origin for them (moomoo's
fundamental endpoints, #771) plus a declared reconciliation policy per family — the loader
reads origins from the observations, so a new origin needs no change here beyond its policy.
