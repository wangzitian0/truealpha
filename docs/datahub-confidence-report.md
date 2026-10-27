# DataHub Confidence & Accuracy Report

The owner's standard (2026-09-15): *"the report is accurate; HIGH confidence = multiple
sources agree, MEDIUM = multiple sources present, LOW = covered by one source; several
metrics at each level; ≥3 sources connected."*

The report holds no band word (#1517). A cell holds `confidence` (one decimal from 0 to 1),
`independent_origins` and `reason`. `confidence_band` (`truealpha_contracts.confidence_bands`)
gives a confidence its display band when a reader serves it. A report written before #1517
keeps its band words.

`apps/data-engine/src/data_engine/datahub/confidence_report.py` grades every
(metric family, subject) cell of the run a governed pointer heads, aggregates per family and
per universe, samples ten subjects across every origin, runs the accuracy oracle, and
appends one content-addressed row to `mart.datahub_confidence_report`. The Dagster job
`datahub_confidence_report` (`lanes/quality.py`) runs it nightly at 00:45 UTC — after
`output_invariants_check` (00:15) — once per lane universe (`universe-list:qqq`, `topt`),
and can be launched by name with an explicit `executed_at`. `/admin/datahub` renders the
newest report per universe.

**Identity is the graded content, not the compile (#885).** `report_id` is the sha256 of
the canonical payload *without* `generated_at` (`confidence_report.COMPILE_STAMPS`). A
nightly compile over an unchanged head that grades exactly what the last one graded
resolves to the existing row (`on conflict do nothing`), so the table grows by one row
per changed report, not one per night; the stored `generated_at` names the compile that
first produced that content. Anything that changes a grade, a sample value or the SEC
oracle's answer is a new row.

## Cell reasons

| reason | rule |
|---|---|
| `independent_origins_agree` | three independent origin groups asserted a value and the family's declared, content-addressed reconciliation policy graded them `agreed` (`reconcile_source_assertions`, #343); the index-membership plane also gives it when both routes list the listing |
| `two_origins_agree` | two independent origins asserted a value and the policy graded them `agreed` |
| `no_agreement_policy` | at least two independent origins asserted a value, and no agreement policy exists for the family |
| `not_agreed_within_tolerance` | two independent origins disagree beyond tolerance and the engine abstained |
| `cross_origin_conflict` | origins disagree beyond tolerance and the engine served the priority source |
| `same_lineage` | at least two origins asserted a value, and they share one lineage |
| `single_origin` | exactly one origin asserted a value. Variants: `served_by_failover` when the primary could not serve the cell and the lone origin is the registered origin that did (#862); `second_origin_other_day` when a session-bound second origin published a different day (#622); `second_origin_other_period` when a period-bound second origin never published the primary's fiscal period (#866); `single_eligible_origin` when the engine found one eligible origin |
| `no_origin_value` | no origin asserted a value (`not_knowable_at_cutoff` and `no_eligible_origin` when the engine found none knowable or eligible). The cell confidence is 0 |

**An origin asserts once.** Two rows of one origin in a run (a retried capture and a
reused binding, #635) are one origin: of the rows on the served day (or period), the
origin asserts from its newest by `knowable_at`, then observation id — the representative
the fusion engine picks — so it gives `single_origin`, never
`same_lineage` with itself (#885). Session days are the instants' UTC days, whatever time
zone the database session renders `timestamptz` in.

**Independence is lineage, not origin id.** A mirror, a reseller, or a second parser of the
same document never corroborates its original (docs/datahub-quality-report.md, step 3). The
headcount plane's two producers (`10k-extraction`, `manual-review`) both read the issuer's
10-K and are one lineage: equal values give `same_lineage`, never an agreement reason.

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
| `pre_provision_profit`, `shares_outstanding` | financial-fact | `origin:sec-company-facts:v1` | none — a single origin gives `single_origin`, two lineages give `no_agreement_policy` |
| `headcount` | financial-fact | `origin:headcount:<producer>` per producer in `staging.issuer_headcount_facts` | none — one lineage gives `same_lineage` |
| `index_membership` | index-membership plane (QQQ) | `origin:nasdaq-index:v1` (`staging.etf_constituent_facts`), `origin:nport:v1` (`mart.fund_holdings_resolved`) | `index-membership-fusion:v1` (new), presence compared exactly |
| `etf_weight` | index-membership plane (QQQ) | `origin:nport:v1` today; `origin:nasdaq-index:v1` once the operator route carries a weight | `index-membership-fusion:v1`, weights compared at the stated tolerance |

**Every field of the bar is its own family (#865).** The close is read under the origin's
registered value key (the v1 second origin wrote `price`); open, high, low and volume under
their own names, which is how every bar-carrying vintage writes them. An origin whose
payload lacks a field — Twelve Data v2 carried the close alone — asserts nothing for that
family: it is present with no value, so the cell is `single_origin`, never a
conflict. A payload without a close asserts no bar at all, exactly as the quality report
reads it, so the two reports grade the same assertions field by field. The families follow
the quality report's per-field fusion (#850): open/high/low/close share the price policy
and volume has its own, so "how many metrics agree" is five bar fields, not one.

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
- Agreed within 1 % → `two_origins_agree`; beyond → `not_agreed_within_tolerance`; the second
  origin never published the primary's period → `second_origin_other_period` (its newest
  figure is recorded and excluded, never compared — the financial analogue of #622); a second
  origin in another currency is present with no comparable value → `single_origin`; a primary
  figure without a dated period
  cannot be aligned and is not corroborated (the quality report compares it no more).
- Two readers of one lineage give `same_lineage` under the policy: a mirror of
  the SEC facts never corroborates them.
- `headcount` keeps its plane rule (10-K extraction and manual review are one lineage,
  `same_lineage`); `pre_provision_profit` and `shares_outstanding` have no second origin
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
  `agreed`; one route only → `single_origin` (a name added since the last quarterly filing, or a
  line held but dropped from the index, is a real single-origin cell — the denominator is
  the union of both routes).
- **Weights** (`etf_weight`, same policy) are compared at absolute 0.05 percentage points
  + 10 % relative. A quarter-end filed weight and an operator weight taken weeks later
  differ by price drift (a few percent); the tolerance exists to catch a mis-joined line
  (an order of magnitude), not drift. Today `staging.etf_constituent_facts.weight` is NULL
  by design, so the weight has one origin (N-PORT) and gives `single_origin`; membership itself
  is still corroborated by both routes.
- N-PORT lines that resolve to no listing (`holdings_unresolved_to_listing`) are counted in
  the family's `routes` notes rather than silently dropped.

## Report shape

```
report_version, universe, universe_id, run_id, cutoff, generated_at (not hashed), environment
independence_rule, subjects, sources_connected
families.<family>: cells, by_independent_origins{count: cells}, mean_confidence, compared,
                   agreed, agreement_rate, tolerance, tolerance_policy, origins, reasons [, routes]
cells.<family>.<subject>: reason, origins, independent_origins, outcome, delta,
                          relative_delta, comparison, excluded, confidence
sample.<subject>.<family>: values{origin: value}, delta, relative_delta, comparison, reason, excluded
accuracy.<field>: origins, compared, agreed, agreement_rate, tolerance_policy,
                  quality_report_id, quality_report_cells, matches_quality_report,
                  quality_report_mismatches
                  (open, high, low, close, volume; revenue, gross_profit, net_income, total_assets)
accuracy.sec_oracle: fields, issuers_requested, issuers_compared, rows[], per_field, skipped
metadata.stored_confidence: used_for_bands=false, values_by_semantic, constant_per_semantic
```

`agreement_rate` is `agreed / compared`, where `compared` counts only cells the engine
actually compared (two independent origins under a policy). `mean_confidence` uses the
family's cell count as denominator; a cell with no origin value stays in it with confidence 0.

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

- Agreement reasons: `close` (QQQ 2026-09-14 head: 101/102 agreed; TOPT: 21/21), `open`/`high`/
  `low`/`volume` wherever the second origin wrote the whole bar (Twelve Data v3 and moomoo
  K-line do; v2 observations corroborate the close alone, so those cells are `single_origin`),
  and `index_membership` (QQQ: 101 of 102 constituents are also in the 2026-06-30 N-PORT
  vintage; the one constituent-only name and the one unresolved N-PORT line give
  `single_origin`).
- `same_lineage`: `headcount` where both 10-K producers wrote (one lineage; 20 TOPT issuers on
  the production plane).
- `single_origin`: every fundamental where only SEC company-facts asserted (the statements
  origin is enabled per environment; where it is on, `revenue`/`gross_profit`/`net_income`/
  `total_assets` give `two_origins_agree` where moomoo published the primary's period and
  agreed, `not_agreed_within_tolerance` where it disagreed, `second_origin_other_period`
  where it has not published that period) and `etf_weight` (N-PORT only; the operator route
  carries no weight).
- `no_origin_value`: cells no origin filled (e.g. `pre_provision_profit` outside the financial
  branch, headcount for issuers the plane has not covered).

What raises the remaining fundamentals to an agreement reason: a second, independent origin for them plus
a declared reconciliation policy per family — the loader reads origins from the
observations, so a new origin needs no change here beyond its policy and its alignment.
