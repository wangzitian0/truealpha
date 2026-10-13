# A5 — The Polars AST replaces Qlib as the factor-expression oracle; VectorBT was selected and never adopted

Status: Accepted. Owner decision 2026-09-23, carried out by #969. Supersedes `init.md` rule 25's
selection of Qlib, which had been in force since 2026-07-17.
Date: 2026-09-23
Correction 2026-10-12 (#1239): the repository does not use VectorBT. See Decision 2.
Amended 2026-10-13 (#758): the Polars AST compiler is a test-time oracle in
`libs/factors/tests/support/expression_oracle`. No deployed path imports it. The backtest engine is
`factors.backtest.engine`. See Decisions 1 and 2.

## Context

`init.md` rule 25 named Qlib the selected factor-expression and backtest engine, and four more
places in the same file repeated it: §3's L2 and L4 layer descriptions, the engine table, Gate 1's
acceptance row, and Gate 1's ownership paragraph. Nothing in `init.md`, `vision.md`, `AGENTS.md`
or `docs/` ever mentioned Polars or VectorBT.

The implementation had already forked away from that contract:

- PRs #932–#935 landed a four-layer Polars/VectorBT backtest stack on 2026-09-22. Their commit
  messages referenced four closed, unrelated issues, so the stack arrived with no issue, no scope
  and no acceptance criteria. #944 reverted all four the same day after four independent read-only
  audits found ten High findings.
- #950 re-landed layers 2 and 3 as contracts on 2026-09-22: `truealpha_contracts.ast` and
  `factors.expressions.compiler` (Polars), plus `factors.backtest` (VectorBT). Nothing was wired
  into the Dagster composition root, and no contract document was amended.
- An agent session on 2026-09-21 recorded in its own memory that it had purged Qlib and written
  `ADR-004-polars-vectorbt-stack.md`. Neither happened in this repository: the ADR directory has
  only ever contained A0–A4, and `libs/factors/src/factors/qlib_engine.py` had exactly one commit
  in its entire history (`a767053`, 2026-07-17) and was never deleted.

So by 2026-09-23 the repository held two expression engines with the same six operators, a contract
that mandated the older one, and a decision that existed only in an agent's self-report. A
2026-09-23 audit also measured the consequence: the `qlib` CI job was **skipped in 11 of the last
12 `ci-required` runs**, because `pyqlib` lives in a workspace-excluded project and the tests that
prove Qlib reproduces each factor's Decimal result are gated behind `pytest.importorskip("qlib")`.
The mandated reproducibility proof was nominally required and effectively dormant.

## Decision

1. **The Polars AST replaces Qlib as the factor-expression engine.** Factor expressions are versioned
   `truealpha_contracts.ast` definitions compiled to `polars.Expr`. Amended 2026-10-13 (#758): the
   native Decimal implementations in `libs/factors` are the computation. The compiler is a test-time
   reproducibility oracle in `libs/factors/tests/support/expression_oracle`. The cross-check tests of
   `gross_profit_per_employee`, `price_to_sales` and `peg` compile each expression through it and check
   that it reproduces the Decimal value. After #758 deleted the top-k adapter, no deployed path called
   the compiler, so it moved out of `libs/factors/src`.
2. **VectorBT was selected as the backtest/portfolio-simulation engine and never adopted.** The
   `vectorbt` package was never in `uv.lock`. The backtest engine is `factors.backtest.engine` (#758).
   It is a Decimal simulation that reads its execution, transaction-cost, total-return and sizing rules
   from the strategy definition. Each run pins it by `ENGINE_ID` in its `run_id` (ADR A6, status update
   2026-10-13).
3. **Every engine-independent constraint from the old rule 25 carries over unchanged.** Neither
   engine is a data or semantic authority. Only `libs/factors` adapters may invoke them. They
   receive provenance-neutral inputs projected from durable PIT snapshots and the explicit
   post-decision market-event stream. Neither may crawl or select vintages, resolve membership,
   infer confidence or lineage, combine adjusted prices with explicit actions, or replace Decimal
   monetary logic. Every run pins the factor-expression engine build, its adapter, the strategy and
   the input snapshot. The two exceptions to "unchanged" are the operator set and the backtest
   engine, neither of which any binding pins today (see Consequences).
4. **The native Decimal implementation stays the source of truth for monetary results.** A compiled
   expression is a reproducibility cross-check of that Decimal result, never its replacement. This
   is the same relationship the Qlib binding had, and it is what keeps the repository's
   "never use binary floating point where monetary precision matters" red line intact: the number
   that reaches `mart` is the Decimal one.
5. **Qlib is deleted in the same change that migrates the expressions**, per the repository's
   migrate-and-clean-in-one-change rule. Leaving it as a second, unbound engine is what produced
   this ADR's context in the first place.

## Consequences

- The reproducibility cross-check for `gross_profit_per_employee`, `peg` and `price_to_sales`
  moves from the pinned Qlib runtime to the Polars compiler, and **stops being skippable**.
  `polars` is a first-class dependency, so the assertion runs on every `ci-python` execution
  instead of only when someone edits the workflow files.
- `ci-qlib.yml`, the `qlib` paths filter and the `qlib` job in `ci-required.yml` disappear. The
  unarmed-check class that #956 found and #960 fixed disappears with them, and a replacement test
  asserts the Polars cross-check files are covered by the `python` filter so the same class cannot
  reappear under a new name.
- The operator vocabulary gains `Std` and `Rank`. `Rank` is what cross-sectional top-k selection
  needs and Qlib's builtin registry never had, which is the capability argument for the swap
  independent of everything above.
- What the old engine had and the new one does not: a versioned operator registry. Its identity was
  carried on `StrategyEngineBinding` as `operator_registry_id` / `operator_registry_sha256`, whose
  value was the content hash of `QlibOperatorRegistry`. That object is deleted here, so the field is
  dropped rather than renamed: keeping the digest would publish a hash of something that no longer
  exists, and substituting a placeholder would fabricate one. The Polars AST has no separately
  versioned registry to hash in its place. **Consequence, stated rather than left implicit:** a run
  now pins the *factor-expression* engine build, its adapter, the strategy and the input snapshot,
  but NOT the operator set — and `ExpressionEngineExecutionBinding` is `distribution="polars"`, so
  it does not pin the backtest engine either. Rule 25 has been written to say exactly that instead
  of mandating a pin with no mechanism. Closing both gaps belongs to whoever builds the backtest
  runner (#26, #758 H2). The assertion the dropped field backed — that two engine bindings over one
  definition differ — is preserved by comparing `distribution` and `runtime_artifact_sha256`
  instead.
- The `ExecutionEvidence` type that recorded which exact build produced a cross-check goes with the
  Qlib module. The cross-check itself survives as an ordinary test assertion.
- The S8/S9 Qlib records in `governance/batches/`, `governance/leases/` and
  `governance/evidence/` were frozen history. #1061 deleted them from the tree because nothing
  read them. They stay in git history (`governance/README.md`). This ADR does not rewrite what
  happened.

## What this ADR does not decide

It does not build the multi-cutoff backtest runner. #758 builds it: `data_engine.lanes.backtest`
evaluates `large_model_value_v0` at each quarter-end cutoff and simulates it with
`factors.backtest.engine`. This ADR does not claim that engine is production-ready. #938's three
inter-layer contracts remain the standing acceptance bar for it.
