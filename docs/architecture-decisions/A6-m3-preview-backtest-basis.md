# A6 — The first M3 backtest run is a preview on split-adjusted bars

Status: Accepted. Agent decision, 2026-10-09. A bounded deviation from `init.md` rule 16.
It expires when #1121 lands.
Date: 2026-10-09
Extends: A5 (Polars AST and VectorBT).
Refs: #758 (M3), #1121, #1122, #1123.

## Context

`init.md` rule 16 requires unadjusted bars plus explicit corporate-action lifecycle events.
The repository cannot meet rule 16 today.

- No corporate-action source exists. The repository has no events table and no splits or dividends call.
- `staging.market_prices_daily` holds only `adjust = 'splits'` bars. Production held 15,556 rows on 2026-10-09.
- Unadjusted bars without split events show about -90% NAV on a 10:1 split. That result is wrong.
- A5 forbids combining adjusted prices with separately applied explicit actions.
- The backtest engine was `libs/factors/src/factors/backtest/engine.py`, a numpy daily simulation that filled at the same-day close with 10 + 5 bp of cost written in the engine. #758 replaced it (see the status update).
- No binding pins the backtest engine, the adapter or the data snapshot. `run_id` defaults to `default_snapshot` (`engine.py#L29`).
- `mart.backtest_runs`, `backtest_trades` and `backtest_valuations` hold 0 rows on production.

## Decision

1. Every run before #1121 is a `preview`. The run summary and the report name the price basis `split_adjusted_preview`.
2. Returns use split-adjusted bars. The run applies no explicit events, so A5's ban on mixing adjusted prices with explicit actions holds.
3. The run is a price return. It omits dividends. The strategy definition declares a constant risk-free rate.
4. Market value is the unadjusted close times the as-filed share count. The prices PR adds `adjust = 'none'` bars. Every reader of `staging.market_prices_daily` filters on `adjust`.
5. Fills happen at the next open after the signal date. The run reports a benchmark.
6. The universe is the frozen `universe:topt-us-2026-03-31`. The report prints the survivorship bias this causes.
7. A run pins four values: the engine id, the adapter version (the image git sha), the strategy definition sha256 and a data-snapshot hash. `run_id` derives from them. The value `default_snapshot` is forbidden.
8. The claim ceiling is `preview`. A run is `validated` only after the holdout record of #1122 exists.
9. The deviation ends when #1121 lands. That change removes the split-adjusted returns basis in the same change.

## Consequences

- The report prints these remaining look-ahead risks: a frozen universe; a filed date stamped at midnight; today's concept map applied to old filings; a constant risk-free rate; no dividends; vendor restatement of bars.
- The first nightly run needs no `DecisionSnapshot`, no page and no MCP tool. The H3 surfaces of #758 still need them and stay in scope.
- A5 stays accurate in intent. Its backtest-engine pin is still open: the engine is a numpy adapter, not the `vectorbt` package. Decision 7 is the pin.
- A reader that ignores `adjust` flips between adjusted and unadjusted bars. A test in the prices PR fails when two `adjust` values coexist for one symbol and date.
- Owner of this deviation: the `truealpha-bt` lane. Expiry condition: #1121 is merged and deployed.

## Status update 2026-10-13 (#758)

The nightly lane now backtests `large_model_value_v0`, not `topt_gppe_rank`. This is the state of
each decision:

- Decisions 1 and 2 no longer hold. The run prices returns on unadjusted bars
  (`adjust = 'none'`). Split events come from the ratio of the unadjusted and the split-adjusted
  closes (`factors.backtest.corporate_actions`). Each split multiplies the held shares once, at
  its ex-date. Adjusted prices never enter a return. `mart.backtest_runs.price_basis` records
  `unadjusted_open_explicit_splits`. A ratio step that the unadjusted series does not confirm
  stops the run.
- Decision 3 holds. No dividend source exists. `mart.backtest_runs.claim_ceiling` records
  `price_return_no_dividends`. The risk-free rate is the constant `RISK_FREE_RATE` of the live path.
- Decision 4 holds. Market value at each cutoff is the unadjusted close times the as-filed share
  count, from the projector's `last_close` and `shares_outstanding` inputs. The backtest reads
  each price series by its `adjust` value.
- Decision 5 is done. Orders fill at the open of the next session after the cutoff's calendar day,
  at the definition's per-side cost. Each run stores an equal-weight buy-and-hold benchmark
  (`benchmark_rule`, `benchmark_cagr`, `benchmark_max_dd`,
  `mart.backtest_valuations.benchmark_cum_nav`). Its universe is the issuers with inputs at the
  first cutoff. A benchmark symbol without an open on the first fill session is bought at its next
  open, and the run records the delay.
- Decision 6 no longer holds. No frozen universe list exists. The universe is the issuers that hold
  strategy inputs today, resolved through today's stored lineage. That is a survivorship bias, and
  each run stores it in `metrics_payload.limitations`. The page and the MCP response show it.
- Decision 7 is done. `run_id` hashes the engine label, the deployed code identity
  (`GIT_COMMIT_SHA`, the value `/api/health` reports), the definition hash, the input snapshot,
  the issuer mapping, the benchmark universe, the price snapshot and the run parameters. The input
  snapshot comes from the same read as the decisions' inputs. A same `run_id` with a different
  result stops the run.
- Decision 8 holds. No holdout record exists, so no run is `validated`.
- Decision 9 is done in part. #758 removed the split-adjusted returns basis without #1121: the
  split events come from the two stored series, not from a corporate-action source. Dividends
  still need a source, so the claim ceiling stays a price return.
- The cutoffs are the definition's calendar quarter ends. The projector writes the inputs as of
  each quarter end. `first_cutoff_at` anchors the live schedule, not the historical window.
