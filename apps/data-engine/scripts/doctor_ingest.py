"""Health check for Postgres, S3 storage, and OpenD."""

import argparse
import socket
import sys
from datetime import UTC, datetime

REQUIRED_TABLES = [
    "raw.fetches",
    "staging.kg_entities",
    "staging.kg_identifiers",
    "staging.kg_edges",
    "staging.fund_holding_facts",
    "staging.api_call_ledger",
]


def check_database() -> list[str]:
    import psycopg
    from data_engine.config import settings

    problems = []
    try:
        with psycopg.connect(settings.database_url, connect_timeout=settings.database_connect_timeout_seconds) as conn:
            for table in REQUIRED_TABLES:
                if conn.execute("select to_regclass(%s)", (table,)).fetchone()[0] is None:
                    problems.append(f"missing table {table} (run the migrations: make db-migrate or docker-exec psql)")
    except Exception as e:
        problems.append(f"cannot connect: {e}")
    return problems


def check_object_storage() -> list[str]:
    import psycopg
    from data_engine import raw_store
    from data_engine.config import settings
    from truealpha_contracts.models import DataSource

    try:
        with psycopg.connect(settings.database_url, connect_timeout=settings.database_connect_timeout_seconds) as conn:
            fetch_id = raw_store.insert_fetch(
                conn,
                source=DataSource.SEC,
                source_record_id="doctor:smoke",
                body=b"doctor smoke payload",
                content_type="text/plain",
                fetched_at=datetime.now(UTC),
                metadata={"doctor": True},
            )
            if raw_store.get_payload(conn, fetch_id) != b"doctor smoke payload":
                return ["S3 round-trip mismatch"]
            conn.commit()  # keep the pointer row: content-addressed, reruns dedupe onto it
    except Exception as e:
        return [f"raw_store round-trip failed: {e}"]
    return []


def check_opend() -> list[str]:
    from data_engine.config import settings

    try:
        if not settings.moomoo_opend_host or not settings.moomoo_opend_port:
            return ["OpenD coordinates are not configured (MOOMOO_OPEND_HOST / _PORT)"]
        with socket.create_connection((settings.moomoo_opend_host, settings.moomoo_opend_port), timeout=3):
            return []
    except OSError as e:
        return [f"OpenD not reachable at its configured address ({type(e).__name__})"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-opend", action="store_true", help="fail (not just report) when OpenD is down")
    args = parser.parse_args()

    from data_engine.config import settings

    print(
        f"env: APP_ENV={settings.app_env}  db={settings.database_url.rsplit('@', 1)[-1]}  s3={'configured' if settings.s3_endpoint else 'UNCONFIGURED'}"
    )
    failed = False
    for name, problems, required in (
        ("postgres+schema", check_database(), True),
        ("object storage round-trip", check_object_storage(), True),
        ("moomoo OpenD tcp", check_opend(), args.require_opend),
    ):
        status = "OK" if not problems else ("FAIL" if required else "WARN")
        failed = failed or (required and bool(problems))
        print(f"[{status}] {name}" + ("".join(f"\n       - {p}" for p in problems)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
