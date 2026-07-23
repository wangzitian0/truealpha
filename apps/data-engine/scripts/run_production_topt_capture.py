"""Manual one-shot TOPT capture (thin wrapper over the schedulable pipeline).

The real logic lives in `data_engine.datahub.live_topt_pipeline` — the same module
the deployed Dagster job (#27) runs per schedule tick. This wrapper keeps the
manual, confirmation-free CLI path for prod bootstrap/backfill use.

Usage (against the DATABASE_URL in settings, or DATABASE_URL env):
    uv run --package truealpha-data-engine python \
      apps/data-engine/scripts/run_production_topt_capture.py [--cutoff ISO] [--version NAME]

`--version` must be unique per (cutoff) rerun — record identities derive from it.
Default derives from the current UTC time so accidental reruns never collide.

truealpha#271 (2026-07-22 follow-up): this wrapper used to stop after capture +
core materialization, so a manual production run never advanced
`mart.current_pointer` -- only the scheduled Dagster tick's `run_topt_live_tick`
op called `register_run_evidence` (`dagster_defs.py`), which this script
deliberately bypasses (it must run without a Dagster deployment). That left every
manual production run resolvable only through `PostgresToptGppeRepository`'s
acceptance-gated fallback query, never the governed `current_pointer_head` path --
silently defeating the #429/#434 P4 exit criterion (MCP and the App must agree via
the SAME governed head) for any consumer reading production. Mirrors the op's
`register_run_evidence` call, in the same transaction, so a manual run is a
governed head too. Deliberately does NOT mirror the op's
`seed_strategy_inputs_from_capture`/`run_strategy_replay_for_cutoff` steps --
the large_model_value_v0 strategy layer in production is explicit non-goal scope
for truealpha#475.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import psycopg
from data_engine.config import settings
from data_engine.datahub.a1_evidence import register_run_evidence
from data_engine.datahub.live_topt_pipeline import run_live_topt_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", default="2026-04-02T00:00:00+00:00", help="PIT cutoff (ISO 8601, tz-aware)")
    parser.add_argument("--version", default=None, help="unique run version label (default: manual-<utcnow>)")
    args = parser.parse_args()

    cutoff = datetime.fromisoformat(args.cutoff)
    version = args.version or f"manual-{datetime.now(UTC):%Y%m%dT%H%M%S}"

    with psycopg.connect(settings.database_url, autocommit=False) as connection:
        print(f"== live TOPT pipeline: cutoff={cutoff.isoformat()} version={version} ==")
        result = run_live_topt_pipeline(connection, cutoff=cutoff, version=version)
        # #378/truealpha#271: register the run on the A1 evidence plane and advance
        # the governed pointer inside the same transaction as the capture, so a
        # manual production run is resolvable through mart.current_pointer_head the
        # moment this commits -- same guarantee the scheduled tick makes.
        pointer_sequence = register_run_evidence(
            connection, run_id=result.run_id, release_manifest_id=result.release_manifest_id
        )
        connection.commit()

    print(f"== run {result.run_id} ==")
    print(f"== materialized {result.core_result_count} core results ==")
    print(f"== quality report {result.quality_report_id} ==")
    print(f"== current_pointer sequence {pointer_sequence} ==")
    for key, value in result.quality.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
