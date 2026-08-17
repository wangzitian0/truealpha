"""Refresh an ETF universe's constituent plane and (optionally) publish its head.

The operator/scheduled entrypoint for #539's data-driven universes: fetch the
index operator's constituents, land bytes + rows with lineage, and publish a
new governed list version when the membership actually changed. Adding an ETF
is a `UNIVERSE_SOURCES` entry, not a new script.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/refresh_etf_universe.py qqq [--publish] [--report-date 2026-06-30] [--note "..."]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import psycopg
from data_engine.config import settings
from data_engine.datahub.production_topt.universe_plane import (
    UNIVERSE_SOURCES,
    current_head_mapping_sha,
    publish_universe_list,
    refresh_etf_constituents,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("etf", choices=sorted(UNIVERSE_SOURCES))
    parser.add_argument("--publish", action="store_true", help="publish the head if the mapping changed")
    parser.add_argument("--report-date", default=None, help="universe report date (default: today UTC)")
    parser.add_argument("--note", default="operator refresh")
    args = parser.parse_args()

    source = UNIVERSE_SOURCES[args.etf]
    report_date = (
        datetime.strptime(args.report_date, "%Y-%m-%d").date() if args.report_date else datetime.now(UTC).date()
    )
    with psycopg.connect(settings.database_url) as connection:
        landed = refresh_etf_constituents(connection, source, openfigi_api_key=settings.openfigi_api_key)
        print(f"{args.etf}: {landed} constituent rows landed")
        if args.publish:
            from data_engine.datahub.production_topt.universe_plane import build_denominator

            fresh_sha = build_denominator(connection, source, report_date=report_date)["instrument_mapping_sha256"]
            if fresh_sha == current_head_mapping_sha(connection, source):
                print("membership unchanged; head not advanced")
            else:
                contract_id, sequence = publish_universe_list(
                    connection, source, report_date=report_date, note=args.note
                )
                print(f"published {contract_id} at sequence {sequence}")
        connection.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
