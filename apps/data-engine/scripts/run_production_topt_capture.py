"""Manual one-shot TOPT capture (thin wrapper over the schedulable pipeline).

The real logic lives in `data_engine.datahub.live_topt_pipeline` — the same module
the deployed Dagster job (#27) runs per schedule tick. This wrapper keeps the
manual, confirmation-free CLI path for prod bootstrap/backfill use.

Usage (against the DATABASE_URL in settings, or DATABASE_URL env):
    uv run --package truealpha-data-engine python \
      apps/data-engine/scripts/run_production_topt_capture.py [--cutoff ISO] [--version NAME]

`--version` must be unique per (cutoff) rerun — record identities derive from it.
Default derives from the current UTC time so accidental reruns never collide.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import psycopg
from data_engine.config import settings
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
        connection.commit()

    print(f"== run {result.run_id} ==")
    print(f"== materialized {result.core_result_count} core results ==")
    print(f"== quality report {result.quality_report_id} ==")
    for key, value in result.quality.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
