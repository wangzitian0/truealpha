"""Health check for Postgres, S3 storage, and OpenD."""

import argparse
import socket
import sys
from datetime import UTC, datetime
from urllib.parse import urlsplit

from truealpha_runtime.checks import (
    DatabaseCheck,
    DependencyStatus,
    ObjectStorageCheck,
    redact_postgres_exception,
)

REQUIRED_TABLES = [
    "raw.fetches",
    "staging.entities",
    "staging.entity_aliases",
    "staging.entity_relations",
    "staging.fund_holding_facts",
    "staging.api_call_ledger",
]


def _safe_database_target(database_url: str) -> str:
    parsed = urlsplit(database_url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    return f"{host}{port}{path}" or "<unparsed>"


def check_database() -> list[str]:
    import data_engine.db as db
    from data_engine.config import settings

    probe = DatabaseCheck(settings).probe()
    if probe.status is not DependencyStatus.PRESENT:
        return [probe.detail or "database unreachable"]

    problems = []
    try:
        with db.connect() as conn:
            for table in REQUIRED_TABLES:
                row = conn.execute("select to_regclass(%s)", (table,)).fetchone()
                if row is None or row[0] is None:
                    problems.append(f"missing table {table} (run the migrations: make db-migrate or docker-exec psql)")
    except Exception as exc:
        redacted = redact_postgres_exception(
            exc,
            database_url=settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        problems.append(f"cannot inspect tables: {redacted}")
    return problems


def check_object_storage() -> list[str]:
    import data_engine.db as db
    import data_engine.raw_store as raw_store
    from data_engine.config import settings
    from truealpha_contracts.models import DataSource

    probe = ObjectStorageCheck(settings).probe()
    if probe.status is not DependencyStatus.PRESENT:
        return [probe.detail or "object storage unreachable"]

    try:
        with db.connect() as conn:
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
    except Exception as exc:
        redacted = redact_postgres_exception(
            exc,
            database_url=settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        return [f"raw_store round-trip failed: {redacted}"]
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
        f"env: ENVIRONMENT={settings.environment}  db={_safe_database_target(settings.database_url)}  "
        f"s3={'configured' if settings.s3_endpoint else 'UNCONFIGURED'}"
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
