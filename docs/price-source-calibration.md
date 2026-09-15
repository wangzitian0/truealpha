# Independent Price-Source Calibration

The repository currently contains Yahoo chart price bars for DDOG, DUOL, NICE,
and SHOP. SEC and moomoo samples are not price-bar providers and must not be
counted as independent price corroboration. Twelve Data responses were observed
on 2026-07-14, and their hashes were recorded in
`apps/data-engine/samples/prices/twelve_data_reconciliation_20260714.json`.
`prices.source_reconciliation` passes the aggregate-report audit with one case,
but that audit does not replay the missing independent response bytes.

The reproducible protocol is recorded in
`apps/data-engine/samples/prices/independent_reconciliation.v1.json`; the
2026-07-14 run is recorded in
`apps/data-engine/samples/prices/twelve_data_reconciliation_20260714.json`.
The raw Twelve Data response bytes are not retained, so this aggregate report is
not a replayable second-source sample and must not independently raise
confidence. The run covers all 754 Yahoo trading dates from 2023-07-10 through 2026-07-10
for DDOG, DUOL, NICE, and SHOP. OHLC prices are compared at 5 bps relative
tolerance; volume at 1%. Volume disagreements are retained in the report,
not hidden by shrinking the denominator. Corporate actions are compared
separately, and both pre- and post-public-availability cutoffs are required.
Missing rows are failures, not denominator shrinkage.

Confidence must not be calibrated from Yahoo's one-year/three-year overlap:
that check only proves same-provider vintage stability. Confidence promotion
requires independent observations, a recorded disagreement rate by field, and
an explicit rule for missing, stale, or conflicting bars. The v1 protocol fixture
(`independent_reconciliation.v1.json`) still records its original
`blocked_missing_independent_capture` state as history; the executable audit is the
current source of truth.

## Third origin: moomoo OpenD daily K-line (`moomoo-kline:v1`)

OpenD runs on the VPS and the data-engine containers reach it over the host network,
yet until this origin existed `staging.api_call_ledger` held zero moomoo rows. The
origin (`apps/data-engine/src/data_engine/datahub/production_topt/moomoo_origin.py`)
asks `request_history_kline` for a bounded daily window ending on the price cutoff —
the last settled session (#637) — UNADJUSTED (`AuType.NONE`) and regular-session only
(`extended_time=False`), and refuses any bar stamped with an instant or dated after the
cutoff (#535). It is fused by `market-price-fusion:v3` with priority yahoo, twelve-data,
moomoo at the unchanged 0.3% tolerance; a disagreeing origin still abstains the cell.

The SDK speaks protobuf and returns DataFrames, so the landed bytes are the decoded bars
in canonical JSON under `raw/moomoo/`, and the parser identity (`moomoo-kline-parser:v1`)
names that rendering. No K-line bytes have been captured yet: the first scheduled
staging tick with `MOOMOO_KLINE_ORIGIN_ENABLED=true` is the cassette this origin's
`test_real_vendor_bytes` entry must be sha-anchored to (A3 admission item 2).

Quota: moomoo's historical-candlestick quota counts distinct stocks per rolling 30-day
window (2,000), not calls; the governed universes use ~120. Every call is still gated by
`moomoo_ledger` (8 per 30 s, monthly backstop) and recorded.

## Second financial-fact origin: moomoo statements (`moomoo-financials:v1`)

`get_financials_statements` (income statement + balance sheet, two gated calls per
issuer per run) corroborates the SEC company-facts primary per field under
`financial-fact-fusion:v1` (`quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY`):
priority sec-company-facts, moomoo-financials; relative tolerance **1%**; abstain on
conflict. Alignment is on the primary's fiscal `period_end` — moomoo stamps a period end
at 00:00 Asia/Shanghai, whose UTC rendering is the day before, so the epoch is converted
in that zone. The field-id map (income 8001 revenue, 8004 gross profit, 8037 net income,
8047/8048 basic/diluted EPS; balance sheet 8001 total assets) was established by equality
against the XBRL facts of the four captured issuers.

Measured on `samples/moomoo/*.json` vs `samples/sec/*.json` (DDOG, DUOL, NICE, SHOP;
FY2023–FY2025), the standing check `test_moomoo_statements_equal_the_sec_facts_within_the_declared_tolerance`:

| field | max relative deviation | note |
|---|---|---|
| revenue | 0 | byte-equal |
| gross profit | 0 | byte-equal |
| total assets | 0 | byte-equal |
| EPS basic / diluted | 0 | byte-equal; the primary carries no EPS yet, so it is captured but not reconciled |
| net income | 0.70% (NICE FY2024) | moomoo reports `ProfitLoss` (before minority interest), the primary `NetIncomeLoss` |

1% clears the definitional gap with margin while a wrong period, currency or units error
lands orders of magnitude outside it. moomoo publishes no filing date, so its assertion is
knowable when served (the cutoff) and only periods that ended at/before the cutoff are
asserted; the fusion compares only the period the primary has filed.
