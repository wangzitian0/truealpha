"""Produce and persist a row-complete DataHub quality report for a capture run (#61 / #404).

Thin wrapper over `data_engine.datahub.quality_report` — the same functions the
deployed Dagster pipeline (#27) persists a report through on every tick.

Usage (against the DATABASE_URL in settings, or DATABASE_URL env):
    uv run --package truealpha-data-engine python apps/data-engine/scripts/datahub_quality_report.py [run_id]
"""

from __future__ import annotations

import sys

import psycopg
from data_engine.config import settings
from data_engine.datahub.quality_report import build_report, latest_run, persist


def main() -> int:
    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        run_id = sys.argv[1] if len(sys.argv) > 1 else latest_run(conn)
        report = build_report(conn, run_id)
        report_id = persist(conn, report)
        conn.commit()
        print(f"quality report {report_id}")
        for key, value in report.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
