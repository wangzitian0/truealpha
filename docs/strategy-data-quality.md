# Strategy Data Quality Blueprint

This document defines when data is sufficient to build tooling, run a local
backtest, or evaluate a strategy. The executable source of truth is
`truealpha_contracts.STRATEGY_DATA_REQUIREMENTS`; this document explains the
boundary.

## Required Data

Every backtest needs point-in-time entity identifiers, adjusted daily OHLCV,
corporate actions, historical universe membership, and immutable financial
vintages. These inputs must retain `knowable_at`, confidence, and raw lineage.
Historical symbol changes and delistings are required to prevent survivorship
bias. Splits and dividends are required to calculate total return.

The seven modules add these inputs:

| Module | Required strategy data |
|---|---|
| PEG | Point-in-time price, earnings/valuation facts, and switchable growth inputs |
| Gross profit per employee | Gross profit, total headcount, issuer type, and filing-text extraction evidence |
| Supply chain | Time-bounded `supplies_to` edges with confidence and raw filing evidence |
| Analyst backtesting | Analyst identity, rating, target price, recommendation time, corroborated public time, and prices |
| ETF virtual company | Point-in-time N-PORT weights, entity resolution, constituent facts, and corporate actions |
| Pure-blood screening | Product/geography segment revenue, semantic taxonomy, extraction confidence, and source text |
| Three-tier valuation | Versioned outputs from upstream base factors at one as-of boundary |

Factors remain provenance-blind. Source selection, reconciliation, and lineage
are resolved before a `(entity_id, value, confidence, as_of)` value enters
`libs/factors`.

## Readiness Gates

`toolchain` means the checked-in corpus is diverse enough to implement and test
parsers, normalization, lineage, PIT guards, identifier fallback, and quality
reporting. It does not authorize performance claims.

`local_backtest` additionally requires at least three years of prices, a
seven-company cross-industry universe, split/dividend golden cases, historical
membership, a restatement pair, corroborated analyst knowability, replayable
supply-chain edges, and composite-factor replay fixtures.

`strategy_evaluation` additionally requires at least five years of prices and
a primary market-data source reconciled against an independent fallback. Real
staging/prod runs must also satisfy the environment acceptance gates in
`init.md`; checked-in fixtures alone cannot authorize production data.

## Current Decision

The 2026-07 sample is sufficient for toolchain development. It is not
sufficient for a trustworthy local backtest or strategy evaluation. Run the
executable audit after every sample change:

```bash
make sample-audit
```

Non-inferable evidence is declared in
`apps/data-engine/samples/strategy_coverage.json`. A flag may only become true
when the referenced golden fixture or corroborating evidence is committed.
