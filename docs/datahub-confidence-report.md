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
| `low` | exactly one origin asserted a value (`single_origin`; `second_origin_other_day` when a session-bound second origin published a different day, #622; `second_origin_other_period` when a period-bound second origin never published the primary's fiscal period, #866) |
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
| `open`, `high`, `low`, `close` | market-price | `origin:yahoo:v1`, `origin:twelve-data:v1`, `origin:moomoo-kline:v1` (when the flag is on) | `market-price-fusion:v3` (`quality_report.RECONCILIATION_POLICY`, 30 bp relative), unit USD, served-day narrowing; one family per bar field (#865) |
| `volume` | market-price | the same origins | `market-volume-fusion:v1` (`quality_report.VOLUME_RECONCILIATION_POLICY`, 2 % relative), unit shares, served-day narrowing |
| `revenue`, `gross_profit`, `net_income`, `total_assets` | financial-fact | `origin:sec-company-facts:v1`, `origin:moomoo-financials:v1` (when the flag is on) | `financial-fact-fusion:v1` (`quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY`, 1 % relative), aligned on the primary's fiscal period end in the primary's currency (#866) |
| `pre_provision_profit`, `shares_outstanding` | financial-fact | `origin:sec-company-facts:v1` | none — single origin grades `low`, two lineages `medium` (`no_agreement_policy`) |
| `headcount` | financial-fact | `origin:headcount:<producer>` per producer in `staging.issuer_headcount_facts` | none — one lineage, `medium` at most |
| `index_membership` | index-membership plane (QQQ) | `origin:nasdaq-index:v1` (`staging.etf_constituent_facts`), `origin:nport:v1` (`mart.fund_holdings_resolved`) | `index-membership-fusion:v1` (new), presence compared exactly |
| `etf_weight` | index-membership plane (QQQ) | `origin:nport:v1` today; `origin:nasdaq-index:v1` once the operator route carries a weight | `index-membership-fusion:v1`, weights compared at the stated tolerance |

**Every field of the bar is its own family (#865).** The close is read under the origin's
registered value key (the v1 second origin wrote `price`); open, high, low and volume under
their own names, which is how every bar-carrying vintage writes them. An origin whose
payload lacks a field — Twelve Data v2 carried the close alone — asserts nothing for that
family: it is present with no value, so the cell is `low` (`single_origin`), never a
conflict. A payload without a close asserts no bar at all, exactly as the quality report
reads it, so the two reports grade the same assertions field by field. The families follow
the quality report's per-field fusion (#850): open/high/low/close share the price policy
and volume has its own, so "how many metrics are HIGH" is five bar fields, not one.

Each bar field is exactly what the quality report calls agreed for that field: the report
re-runs the same engine over the same observations and records, per field, whether its
per-listing outcomes match `reconciliation_cells[*].fields[<field>].outcome` in the
persisted `mart.datahub_quality_report` row for the run
(`accuracy.<field>.matches_quality_report`; the close is what the pointer gate reads). A
cell this report never compared while the quality report graded it `agreed` or
`conflict_abstained` is a mismatch. A quality report persisted before the bar was fused
per field is compared on the close alone; the other fields say `null` rather than a
vacuous match.

### Financial-fact policy (#866)

The four fundamentals the quality report fuses (#854) reconcile here under the same
`FINANCIAL_FACT_RECONCILIATION_POLICY`, aligned the same way (`confidence_report.financial_origins`
mirrors `quality_report.reconcile_financial_fact_entries`):

- The primary (SEC company-facts) asserts its figure at its own fiscal period end
  (`primary_financial_fields`). A second origin asserts, for each field, its figure **at the
  primary's period end** (`by_period_end[<period>][<field>]`, `corroborating_financial_value`)
  in the primary's reporting currency (`financial_fact_unit`).
- Agreed within 1 % → `high` (`independent_origins_agree`); beyond → `medium`
  (`not_agreed_within_tolerance`); the second origin never published the primary's period →
  `low` (`second_origin_other_period`: its newest figure is recorded and excluded, never
  compared — the financial analogue of #622); a second origin in another currency is present
  with no comparable value → `low` (`single_origin`); a primary figure without a dated period
  cannot be aligned and is not corroborated (the quality report compares it no more).
- Two readers of one lineage stay `medium` (`same_lineage`) under the policy: a mirror of
  the SEC facts never corroborates them.
- `headcount` keeps its plane rule (10-K extraction and manual review are one lineage,
  `medium` at most); `pre_provision_profit` and `shares_outstanding` have no second origin
  and no policy.

`accuracy.<field>.matches_quality_report` cross-checks each fused field against
`financial_fact_reconciliation_cells[*].fields[<field>].outcome` in the persisted quality
report for the run.

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
accuracy.<field>: origins, compared, agreed, agreement_rate, tolerance_policy,
                  quality_report_id, quality_report_cells, matches_quality_report,
                  quality_report_mismatches
                  (open, high, low, close, volume; revenue, gross_profit, net_income, total_assets)
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

- **bar fields** (`open`, `high`, `low`, `close`, `volume`) and **fused fundamentals**
  (`revenue`, `gross_profit`, `net_income`, `total_assets`): the origin agreement the engine
  computed per field, with the field's policy and the per-field cross-check against the
  persisted quality report.
- **fundamentals**: `quality.vendor_oracle`'s deliberately independent SEC re-derivation
  (wider concept lists, latest period across variants — it does not import the adapter)
  for a sample of issuers (default 5: the sample subjects first, then the universe in
  order), fetched live through the source gateway. Agreement is reported per field
  (`revenue`, `gross_profit`) over issuers where the vendor asserts a value, with the
  period mart's figure came from and its staleness in years. Without an SEC user agent the
  section says `no_sec_user_agent` and compares nothing.

## What it shows today, honestly

- `high`: `close` (QQQ 2026-09-14 head: 101/102 agreed; TOPT: 21/21), `open`/`high`/`low`/
  `volume` wherever the second origin wrote the whole bar (Twelve Data v3 and moomoo K-line
  do; v2 observations corroborate the close alone, so those cells are `low`), and
  `index_membership` (QQQ: 101 of 102 constituents are also in the 2026-06-30 N-PORT
  vintage; the one constituent-only name and the one unresolved N-PORT line grade `low`).
- `medium`: `headcount` where both 10-K producers wrote (one lineage; 20 TOPT issuers on
  the production plane).
- `low`: every fundamental where only SEC company-facts asserted (the statements origin
  is enabled per environment; where it is on, `revenue`/`gross_profit`/`net_income`/
  `total_assets` grade `high` where moomoo published the primary's period and agreed,
  `medium` where it disagreed, `low` `second_origin_other_period` where it has not
  published that period) and `etf_weight` (N-PORT only; the operator route carries no
  weight).
- `missing`: cells no origin filled (e.g. `pre_provision_profit` outside the financial
  branch, headcount for issuers the plane has not covered).

What raises the remaining fundamentals to `high`: a second, independent origin for them plus
a declared reconciliation policy per family — the loader reads origins from the
observations, so a new origin needs no change here beyond its policy and its alignment.
