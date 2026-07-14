# Independent Price-Source Calibration

The repository currently contains Yahoo chart price bars for DDOG, DUOL, NICE,
and SHOP. SEC and moomoo samples are not price-bar providers and must not be
counted as independent price corroboration. No Twelve Data response bytes are
currently captured, so `prices.source_reconciliation` remains an explicit
blocker.

The reproducible protocol is recorded in
`apps/data-engine/samples/prices/independent_reconciliation.v1.json`; the
2026-07-14 run is recorded in
`apps/data-engine/samples/prices/twelve_data_reconciliation_20260714.json`.
The run covers all 754 Yahoo trading dates from 2023-07-10 through 2026-07-10
for DDOG, DUOL, NICE, and SHOP. OHLC prices are compared at 5 bps relative
tolerance; volume at 1%. Volume disagreements are retained in the report,
not hidden by shrinking the denominator. Corporate actions are compared
separately, and both pre- and post-public-availability cutoffs are required.
Missing rows are failures, not denominator shrinkage.

Confidence must not be calibrated from Yahoo's one-year/three-year overlap:
that check only proves same-provider vintage stability. Confidence promotion
requires independent observations, a recorded disagreement rate by field, and
an explicit rule for missing, stale, or conflicting bars. Until then the
fixture is intentionally `blocked_missing_independent_capture` and emits zero
verified reconciliation cases.
