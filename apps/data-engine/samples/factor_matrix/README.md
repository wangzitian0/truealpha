# Factor-matrix sample captures

This directory freezes replayable daily-price inputs for the existing nine-symbol
development denominator: ADM, DDOG, DUOL, JPM, META, NICE, NVDA, PLUG, and SHOP.
It does not establish TOPT coverage, source reconciliation, confidence, factor
semantics, strategy validity, or release readiness.

`capture_plan.v1.json` is the source of truth for the inclusive
`2023-07-10..2026-07-10` window, provider symbols, request parameters, and field
semantics. Parser fixtures are content-hashed by that plan.

## Capture

Yahoo requires no credential:

```sh
uv run --package truealpha-data-engine python \
  apps/data-engine/scripts/capture_factor_matrix_samples.py \
  --capture-id factor-matrix-yahoo-YYYYMMDD \
  --providers yahoo
```

Twelve Data is opt-in and reads its key only from the process environment:

```sh
TWELVE_DATA_API_KEY=... uv run --package truealpha-data-engine python \
  apps/data-engine/scripts/capture_factor_matrix_samples.py \
  --capture-id factor-matrix-yahoo-twelve-YYYYMMDD \
  --providers yahoo,twelve_data
```

The command fetches and validates the full provider/symbol denominator before it
atomically publishes a capture directory. It retains exact raw response bytes,
safe request metadata, deterministic provider-specific CSVs, and SHA-256 values
in `manifest.json`. Reusing an existing capture ID performs offline validation;
it never refetches or overwrites that point-in-time record. Use a new capture ID
for every new provider observation.

Yahoo OHLC fields remain unadjusted and Yahoo adjusted close is retained in the
separate `adjusted_close` column. Twelve Data is requested with `adjust=none` and
emits an empty `adjusted_close` field. The capture command never combines the two
providers or reconstructs corporate actions.

## Current evidence

`factor-matrix-yahoo-20260715` contains Yahoo raw/request/normalized artifacts for
all nine symbols. Each normalized file has 754 rows spanning the complete frozen
window. Its manifest SHA-256 is
`1e171c0cbeceb2a9ac11903e199a0812bae6824f06c7830a65e8d78cdffe4417`.

This is a single-provider development capture. A nine-symbol Twelve Data capture
has not been claimed because `TWELVE_DATA_API_KEY` was unavailable in the capture
environment. Twelve Data documents the fixed `start_date`/`end_date` behavior and
the `adjust` modes at <https://twelvedata.com/docs/introduction>.
