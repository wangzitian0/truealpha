# Factor-matrix sample captures

This directory freezes replayable daily-price inputs for the existing nine-symbol
development denominator: ADM, DDOG, DUOL, JPM, META, NICE, NVDA, PLUG, and SHOP.
It does not establish TOPT coverage, source reconciliation, confidence, factor
semantics, strategy validity, or release readiness.

`capture_plan.v2.json` is the source of truth for the inclusive
`2023-07-10..2026-07-10` window, provider symbols, request parameters, and field
semantics. Parser fixtures are content-hashed by that plan. The retained v1 plan
exists only to validate captures made before the Twelve Data end-boundary repair.

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
for every new provider observation. HTTP 429 responses are retried once, honoring
a bounded numeric `Retry-After` value when present.

Yahoo OHLC fields remain unadjusted and Yahoo adjusted close is retained in the
separate `adjusted_close` column. Twelve Data is requested with `adjust=none` and
emits an empty `adjusted_close` field. The capture command never combines the two
providers or reconstructs corporate actions.

## Current evidence

`factor-matrix-yahoo-twelve-v2-20260715` contains raw, request, and normalized
artifacts for both providers and all nine symbols. Every normalized file has 754
rows spanning the complete frozen window. Its manifest SHA-256 is
`7e97444e61f8df2bff0e32964bf8d921678d592ec2c984081977879e6a4f14db`.

The retained `factor-matrix-yahoo-twelve-20260715` v1 capture records the finding
that a Twelve Data daily request with `end_date=2026-07-10` ended on July 9 for
all nine symbols. The v2 plan therefore sends the exclusive upper bound July 11
and filters normalized rows to the inclusive sample window. Twelve Data's support
example likewise shows an `end_date` daily request ending on the preceding date:
<https://support.twelvedata.com/en/articles/5214728-getting-historical-data>.

The earlier Yahoo-only `factor-matrix-yahoo-20260715` directory is retained as
invalidated development evidence. It pins the pre-review plan hash `d89ac31f...`,
whose path was superseded before merge, so it is intentionally excluded from E0
and E1 evidence and cannot be validated against the frozen v1 or v2 plan.
