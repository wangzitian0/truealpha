# Independent Price-Source Calibration

The repository currently contains Yahoo chart price bars for DDOG, DUOL, NICE,
and SHOP. SEC and moomoo samples are not price-bar providers and must not be
counted as independent price corroboration. No Twelve Data response bytes are
currently captured, so `prices.source_reconciliation` remains an explicit
blocker.

The reproducible protocol is recorded in
`apps/data-engine/samples/prices/independent_reconciliation.v1.json`. A future
capture must append immutable Twelve Data response artifacts and populate one
observation per symbol/date. Dates are intersected by provider trading
calendar. OHLC prices are compared at 5 bps relative tolerance; volume at 1%.
Corporate actions are compared separately, and both pre- and post-public-
availability cutoffs are required. Missing rows are failures, not denominator
shrinkage.

Confidence must not be calibrated from Yahoo's one-year/three-year overlap:
that check only proves same-provider vintage stability. Confidence promotion
requires independent observations, a recorded disagreement rate by field, and
an explicit rule for missing, stale, or conflicting bars. Until then the
fixture is intentionally `blocked_missing_independent_capture` and emits zero
verified reconciliation cases.
