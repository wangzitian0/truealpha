"""Refresh an ETF universe's constituent plane and (optionally) publish its head.

The operator entrypoint for #539's data-driven universes — the scheduled weekly
job (`universe_refresh_pipeline`) runs the same `refresh_and_publish` function.
Adding an ETF is a `UNIVERSE_SOURCES` entry, not a new script.

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
    latest_quarter_end,
    refresh_and_publish,
    refresh_etf_constituents,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("etf", choices=sorted(UNIVERSE_SOURCES))
    parser.add_argument("--publish", action="store_true", help="publish the head if the mapping changed")
    parser.add_argument(
        "--report-date", default=None, help="universe report date (default: the last completed quarter end)"
    )
    parser.add_argument("--note", default="operator refresh")
    args = parser.parse_args()

    source = UNIVERSE_SOURCES[args.etf]
    report_date = (
        datetime.strptime(args.report_date, "%Y-%m-%d").date()
        if args.report_date
        else latest_quarter_end(datetime.now(UTC).date())
    )
    with psycopg.connect(settings.database_url) as connection:
        if args.publish:
            print(
                refresh_and_publish(
                    connection,
                    source,
                    report_date=report_date,
                    note=args.note,
                    openfigi_api_key=settings.openfigi_api_key,
                )
            )
        else:
            landed = refresh_etf_constituents(connection, source, openfigi_api_key=settings.openfigi_api_key)
            print(f"{args.etf}: {landed} constituent rows landed (no --publish)")
        connection.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
