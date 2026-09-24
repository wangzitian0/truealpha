"""Manual one-shot TOPT capture (thin shell over the schedulable pipeline).

The real logic lives in `data_engine.datahub.production_topt.composition` — the same
module the deployed Dagster job (#27) runs per schedule tick: plan, dispatch every
obligation through the generic executor and its per-semantic adapters, freeze,
materialize and grade. This wrapper keeps the manual, Dagster-free CLI path for
bootstrap/backfill use.

Usage (against the DATABASE_URL in settings, or DATABASE_URL env):
    uv run --package truealpha-data-engine python \
      apps/data-engine/scripts/run_production_topt_capture.py [--cutoff ISO] [--version NAME]

`--version` must be unique per (cutoff) rerun — record identities derive from it.
Default derives from the current UTC time so accidental reruns never collide.

truealpha#271: this wrapper used to stop after capture + core materialization, so a
manual run never advanced `mart.current_pointer` -- only the scheduled Dagster tick's
`run_topt_live_tick` op called `register_run_evidence` (`dagster_defs.py`), which this
script deliberately bypasses (it must run without a Dagster deployment). That left every
manual run resolvable only through `PostgresToptGppeRepository`'s acceptance-gated
fallback query, never the governed `current_pointer_head` path -- silently defeating the
#429/#434 P4 exit criterion (MCP and the App must agree via the SAME governed head).
Mirrors the op's `register_run_evidence` call, in the same transaction, so a manual run
is a governed head too -- including its #536 gate: a manual run whose quality report
misses a declared service objective persists in full and leaves the head where it is.

truealpha#575: this script used to also skip the op's strategy-bridge steps, citing
truealpha#475 as scope -- but #475 was a narrowly-scoped, now-closed 2026-07 bootstrap
milestone ("no strategy/backtest layer"), not a standing architectural boundary. Once the
strategy layer became a governed, relied-upon part of the read path (`/research`, MCP's
`strategy_run`), a manual capture that advances `mart.current_pointer` without binding a
strategy run to it left `mart.governed_strategy_run`'s inner join resolving to nothing --
readers silently fell back to "newest by executed_at", exactly #575's bypass symptom. Now
mirrors the tick's strategy bridge too: `seed_strategy_inputs_from_capture` ->
`persist_strategy_input_coverage` -> `run_strategy_replay_for_cutoff`, same order, same
transaction, same parameters as `_run_tick` (`lanes/capture.py`), before
`register_run_evidence` so the pointer never advances ahead of its binding.

Still does NOT mirror the op's #544 plausibility gate (`judge_run`) -- a separate,
independently-tracked gap, truealpha#1028.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
from data_engine.config import settings
from data_engine.datahub.a1_evidence import register_run_evidence
from data_engine.datahub.production_topt.composition import run_topt_pipeline
from data_engine.datahub.strategy_bridge import (
    persist_strategy_input_coverage,
    run_strategy_replay_for_cutoff,
    seed_strategy_inputs_from_capture,
)

# The GPPE materialization's own risk-free rate (production_topt/composition.py) -- the
# tick's strategy replay pins the same constant, supplied explicitly by the caller.
_RISK_FREE_RATE = Decimal("0.05")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", default="2026-04-02T00:00:00+00:00", help="PIT cutoff (ISO 8601, tz-aware)")
    parser.add_argument("--version", default=None, help="unique run version label (default: manual-<utcnow>)")
    args = parser.parse_args()

    cutoff = datetime.fromisoformat(args.cutoff)
    version = args.version or f"manual-{datetime.now(UTC):%Y%m%dT%H%M%S}"

    with psycopg.connect(settings.database_url, autocommit=False) as connection:
        print(f"== TOPT capture pipeline: cutoff={cutoff.isoformat()} version={version} ==")
        result = run_topt_pipeline(connection, cutoff=cutoff, version=version)
        # truealpha#575: strategy bridge, same order/transaction/parameters as the
        # scheduled tick's TOPT branch (lanes/capture.py's _run_tick), before the pointer
        # advance below -- so a manual run's governed head always has a bound strategy
        # run, the same guarantee the scheduled tick makes.
        seeded = seed_strategy_inputs_from_capture(connection, result.run_id, cutoff=cutoff)
        l2_complete, l2_total = persist_strategy_input_coverage(connection, result.run_id, cutoff=cutoff)
        strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
            connection,
            cutoff=cutoff,
            executed_at=cutoff,
            risk_free_rate=_RISK_FREE_RATE,
            capture_run_id=result.run_id,
        )
        # #378/truealpha#271: bind the run to its release manifest on the A1 evidence
        # plane and advance the governed pointer inside the same transaction as the
        # capture, so the run is resolvable through mart.current_pointer_head the moment
        # this commits -- the same guarantee the scheduled tick makes.
        registration = register_run_evidence(
            connection,
            run_id=result.run_id,
            release_manifest_id=result.release_manifest_id,
            quality_report=result.quality,
        )
        connection.commit()

    print(f"== run {result.run_id} ==")
    print(f"== materialized {result.core_result_count} core results ==")
    print(f"== quality report {result.quality_report_id} ==")
    print(f"== {seeded} strategy inputs seeded; L2 coverage {l2_complete}/{l2_total} ==")
    print(f"== strategy run {strategy_run_id} ({decision_count} decisions, snapshot {snapshot_id}) ==")
    if registration.accepted:
        print(f"== current_pointer sequence {registration.sequence} ==")
    else:
        # #536: the manual path refuses the same advances the scheduled tick refuses.
        print(f"== current_pointer WITHHELD at sequence {registration.sequence}: {registration.summary} ==")
    for key, value in result.quality.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
