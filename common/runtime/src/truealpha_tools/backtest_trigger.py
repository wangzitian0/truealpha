"""CLI entrypoint for queueing governed backtest runs via the single backtest-trigger contract (Q05)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
from truealpha_runtime.backtest_trigger import enqueue_backtest_trigger, prepare_backtest_trigger
from truealpha_runtime.config import RuntimeSettings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue a governed TrueAlpha backtest trigger request.")
    parser.add_argument("--definition-resource", default=None, help="Registered strategy definition JSON resource.")
    parser.add_argument(
        "--custom-definition-file", default=None, help="Path to validated custom StrategyDefinition JSON."
    )
    parser.add_argument("--release-manifest-id", default=None, help="Release manifest identifier binding this trigger.")
    parser.add_argument("--start-cutoff", default=None, help="Exploratory ISO-8601 UTC start cutoff.")
    parser.add_argument("--end-cutoff", default=None, help="Exploratory ISO-8601 UTC end cutoff.")
    parser.add_argument("--executed-at", default=None, help="Override execution timestamp (ISO-8601 UTC).")
    parser.add_argument("--idempotency-key", default=None, help="Stable idempotency key for deduplicated retries.")
    parser.add_argument("--principal-id", default="cli:operator", help="Audit principal identifier.")
    parser.add_argument(
        "--database-url", default=None, help="PostgreSQL DSN (defaults to RuntimeSettings.database_url)."
    )
    args = parser.parse_args(argv)

    custom_def: dict[str, Any] | None = None
    if args.custom_definition_file:
        custom_def = json.loads(Path(args.custom_definition_file).read_text(encoding="utf-8"))
        if not isinstance(custom_def, dict):
            raise SystemExit("custom-definition-file must contain a JSON object")

    prepared = prepare_backtest_trigger(
        definition_resource=args.definition_resource,
        custom_definition=custom_def,
        release_manifest_id=args.release_manifest_id,
        start_cutoff=args.start_cutoff,
        end_cutoff=args.end_cutoff,
        executed_at=args.executed_at,
        idempotency_key=args.idempotency_key,
    )
    db_url = args.database_url or RuntimeSettings().database_url
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            request_id = enqueue_backtest_trigger(cur, principal_id=args.principal_id, prepared=prepared)
        conn.commit()
    print(
        json.dumps(
            {
                "request_id": request_id,
                "dedupe_key": prepared.dedupe_key,
                "run_key": prepared.run_key,
                "executed_at": prepared.executed_iso,
                "custom_definition_sha256": prepared.custom_definition_sha256,
                "release_manifest_id": prepared.release_manifest_id,
                "exploratory": prepared.exploratory,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
