"""Manually materialize GPPE v0 and three-tier results from one exact Production run."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from factors.production_topt import GppeV0Definition

CONFIRMATION = "MATERIALIZE PRODUCTION TOPT CORE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--release-manifest-id", required=True)
    parser.add_argument("--risk-free-rate", required=True, type=Decimal)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != CONFIRMATION:
        parser.error(f"--confirmation must be exactly {CONFIRMATION!r}")

    with psycopg.connect(settings.database_url, autocommit=False) as connection:
        repository = PostgresToptCoreRepository(connection)
        snapshot = repository.freeze_snapshot(
            run_id=args.run_id,
            release_manifest_id=args.release_manifest_id,
        )
        results = repository.materialize(
            snapshot,
            gppe_definition=GppeV0Definition(risk_free_rate=args.risk_free_rate),
        )
        connection.commit()
    output = {
        "run_id": snapshot.run_id,
        "release_manifest_id": snapshot.release_manifest_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_sha256": snapshot.content_sha256,
        "result_count": len(results),
        "available_count": sum(item.availability.value == "available" for item in results),
        "unavailable_count": sum(item.availability.value == "unavailable" for item in results),
        "invocation_id": results[0].invocation_id,
        "gppe_definition_id": results[0].gppe_definition_id,
        "tier_definition_id": results[0].tier_definition_id,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
