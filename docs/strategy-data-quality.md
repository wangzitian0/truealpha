# Strategy Data Quality Blueprint

This document defines readiness of the checked-in research corpus for tooling,
local replay, and later strategy evaluation. It does **not** define module quality,
continuous operation, or Production graduation. The executable source of truth for
these corpus checks is `truealpha_contracts.STRATEGY_DATA_REQUIREMENTS`; the release
gates in `init.md` remain authoritative for the product.

## Required Data

Every backtest needs point-in-time issuer/security/listing identifiers, daily OHLCV,
corporate actions, FX, exchange calendars, historical universe membership, and
immutable financial vintages. These inputs must retain `knowable_at`, `recorded_at`,
confidence, and raw lineage. Historical symbol changes and delistings are required to
prevent survivorship bias. The v1 return policy uses unadjusted bars plus explicit
corporate-action events; adjusted-close evidence must never be combined with those
events to count a split or dividend twice.

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

Factors remain provenance-blind. Source selection, reconciliation, and lineage are
resolved before the runner projects typed semantic records into `libs/factors`.
Factor code sees opaque input IDs, typed subjects, values/units/currencies, relevant
valid time, confidence, and the snapshot cutoff, but never source or raw references.

## Readiness Gates

`toolchain` means the checked-in corpus is diverse enough to implement and test
parsers, normalization, lineage, PIT guards, identifier fallback, and quality
reporting. It does not authorize performance claims.

`local_backtest` additionally requires at least three years of prices, a
seven-company cross-industry universe, split/dividend golden cases, historical
membership, a restatement pair, corroborated analyst knowability, replayable
supply-chain edges, and composite-factor replay fixtures.

`strategy_evaluation` additionally requires at least five years of prices for the
predeclared eligible evaluation cohort and a primary market-data source reconciled
against an independent fallback. The Production research universe is a separate
immutable scope: recent IPOs remain visible under an explicit insufficient-history
policy rather than being deleted to make the evaluation cohort pass. This is a
data-corpus prerequisite, not permission to evaluate or promote a strategy by itself.

The complete release path additionally requires all of the following independent
evidence:

- Gate 0 semantic, executable-contract, source-rights, applicability, coverage, and
  freshness closure (#56-#61).
- The Core filing-to-headcount data plane and blind Core holdout (#70-#71).
- Dedicated longitudinal forecast/analyst and ETF data planes (#62-#63), the generic
  filing/extraction substrate (#64), and the module candidates that consume it.
- A sealed holdout quality gate for every applicable research module (#65).
- Natural-cadence Staging soak, multi-regime strategy validation, recovery, and exact
  release promotion (#49-#53).
- Configuration-only onboarding for an unseen issuer/theme (#72), deployed Production
  consumer validation, and curated-universe shadow graduation (#66-#67).
- The independent row-complete Production capture audit (#68), followed by the final
  Vision audit (#54).

An `unavailable`, stale, excluded, unresolved, or low-confidence result does not count
toward usable module coverage. Checked-in fixtures, two immediate scheduler runs, or a
manually changed readiness flag cannot satisfy any of these release outcomes.

## Current Decision

The targeted 2026-07 evidence set is sufficient for toolchain and local boundary
implementation. The composite-factor replay fixture has since landed and every `local_backtest` row
passes the executable audit (`make sample-audit`). `strategy_evaluation` corpus readiness remains blocked by five-year
coverage for every declared evaluation subject and primary/fallback price
reconciliation. Even after those checks pass, the Gate 0, module holdout, operational,
consumer, and graduation gates above remain blocking. Run the executable corpus audit
after every sample change:

```bash
make sample-audit
```

Non-inferable evidence is declared in
`apps/data-engine/samples/strategy_coverage.json`. Readiness requires a typed
evidence case, immutable artifact hashes, and registered executable assertions;
boolean coverage claims cannot satisfy a gate.
