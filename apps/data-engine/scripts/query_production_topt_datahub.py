"""Bounded downstream reads for exact Production TOPT identities."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository, ToptCoreIdentity
from data_engine.datahub.repository import PostgresCaptureControlRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--read", choices=("status", "meta_info", "core_results", "core_meta_info"), required=True)
    parser.add_argument("--release-manifest-id")
    parser.add_argument("--universe-id")
    parser.add_argument("--universe-version")
    parser.add_argument("--universe-sha256")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--invocation-id")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        output: object
        capture = PostgresCaptureControlRepository(connection)
        if args.read == "status":
            output = asdict(capture.status(args.run_id))
        elif args.read == "meta_info":
            output = [asdict(item) for item in capture.meta_info(args.run_id, limit=args.limit, offset=args.offset)]
        else:
            exact = (
                args.release_manifest_id,
                args.universe_id,
                args.universe_version,
                args.universe_sha256,
                args.snapshot_id,
                args.invocation_id,
            )
            if any(value is None for value in exact):
                parser.error(
                    "core reads require --release-manifest-id, --universe-id, --universe-version, "
                    "--universe-sha256, --snapshot-id, and --invocation-id"
                )
            identity = ToptCoreIdentity(
                run_id=args.run_id,
                release_manifest_id=args.release_manifest_id,
                universe_id=args.universe_id,
                universe_version=args.universe_version,
                universe_sha256=args.universe_sha256,
                snapshot_id=args.snapshot_id,
                invocation_id=args.invocation_id,
            )
            core = PostgresToptCoreRepository(connection)
            reader = core.results if args.read == "core_results" else core.meta_info
            output = [asdict(item) for item in reader(identity, limit=args.limit, offset=args.offset)]
    print(json.dumps(output, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
