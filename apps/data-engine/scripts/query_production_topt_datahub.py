"""Bounded downstream reads for Production TOPT status, metadata, and GPPE."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

import psycopg
from data_engine.config import settings
from data_engine.datahub.repository import PostgresCaptureControlRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--read", choices=("status", "meta_info"), required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        capture = PostgresCaptureControlRepository(connection)
        if args.read == "status":
            output = asdict(capture.status(args.run_id))
        else:
            output = [asdict(item) for item in capture.meta_info(args.run_id, limit=args.limit, offset=args.offset)]
    print(json.dumps(output, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
