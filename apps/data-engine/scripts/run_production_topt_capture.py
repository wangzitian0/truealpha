"""Manually prepare an exact Production TOPT run; no schedule is registered."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from data_engine.config import settings
from data_engine.contract_repository import PostgresReleaseManifestRepository
from data_engine.datahub.production_topt import (
    ManualProductionToptRequest,
    persist_manual_production_plan,
    plan_manual_production_topt,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must be timezone-aware")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--expected-corpus-sha256", required=True)
    parser.add_argument("--release-manifest-id", required=True)
    parser.add_argument("--cutoff", type=_aware_datetime, required=True)
    parser.add_argument("--run-sequence", type=int, default=1)
    parser.add_argument("--confirm-production", required=True)
    args = parser.parse_args()

    corpus_bytes = args.corpus.read_bytes()
    if hashlib.sha256(corpus_bytes).hexdigest() != args.expected_corpus_sha256:
        raise SystemExit("frozen TOPT corpus SHA-256 mismatch")
    with psycopg.connect(settings.database_url, autocommit=False) as connection:
        release = PostgresReleaseManifestRepository(connection).get(args.release_manifest_id)
        if release is None:
            raise SystemExit("accepted ReleaseManifest does not exist")
        plan = plan_manual_production_topt(
            json.loads(corpus_bytes),
            ManualProductionToptRequest(
                release_manifest_id=args.release_manifest_id,
                release=release,
                cutoff=args.cutoff,
                run_sequence=args.run_sequence,
                confirmation=args.confirm_production,
            ),
        )
        status = persist_manual_production_plan(connection, plan, recorded_at=datetime.now(UTC))
    print(json.dumps(asdict(status), default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
