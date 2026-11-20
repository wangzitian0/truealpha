"""Slice 6 gate: retire unread staging views and pin the production ledger column order (#1699)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.testing import apply_migration_chain, guard_db_write

REPO_ROOT = Path(__file__).resolve().parents[3]
RETIRE_MIGRATION = REPO_ROOT / "db/migrations/20261120T1252_runtime_retire_entity_graph_views.sql"
RETIRED_VIEWS = (
    "staging.entity_graph_nodes",
    "staging.entity_graph_edges",
    "staging.entity_alias_resolution",
)
#: Column order of staging.api_call_ledger on production, read on 2026-11-20 12:40 UTC
#: (pg_attribute, attnum order). The legacy chain added fetch_key after reasoning_tokens.
PRODUCTION_LEDGER_COLUMNS = (
    "id", "source", "endpoint", "called_at", "caller", "ok", "status_code", "error",
    "duration_ms", "request_uri", "payload_sha256", "byte_length", "cost",
    "capacity_window_id", "run_key", "attempt", "provider_code", "request_params",
    "completion_tokens", "reasoning_tokens", "fetch_key", "repeat_reason",
)  # fmt: skip


def _admin_url() -> str:
    params = conninfo_to_dict(guard_db_write())
    return make_conninfo(**(params | {"dbname": "postgres"}))


@pytest.fixture
def fresh_db() -> Generator[str]:
    admin_url = _admin_url()
    name = f"truealpha_slice6_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs required DB tests")
    db_url = make_conninfo(**(conninfo_to_dict(admin_url) | {"dbname": name}))
    try:
        apply_migration_chain(db_url)
        yield db_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def _present(conn: psycopg.Connection) -> list[str]:
    return [name for name in RETIRED_VIEWS if conn.execute("select to_regclass(%s)", (name,)).fetchone() != (None,)]


def test_fresh_chain_builds_no_retired_view(fresh_db: str) -> None:
    with psycopg.connect(fresh_db) as conn:
        assert _present(conn) == []


def test_replay_drops_the_retired_views_of_a_live_database(fresh_db: str) -> None:
    # Production and staging hold all three views; the nodes view reads the alias view.
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("create view staging.entity_alias_resolution as select 1 as alias_id")
        conn.execute("create view staging.entity_graph_nodes as select alias_id from staging.entity_alias_resolution")
        conn.execute("create view staging.entity_graph_edges as select 1 as relation_id")
        assert _present(conn) == list(RETIRED_VIEWS)
    apply_migration_chain(fresh_db)
    with psycopg.connect(fresh_db) as conn:
        assert _present(conn) == []


def test_governed_strategy_run_serves_every_universe_family(fresh_db: str) -> None:
    with psycopg.connect(fresh_db) as conn:
        (definition,) = conn.execute("select pg_get_viewdef('mart.governed_strategy_run'::regclass, true)").fetchone()
    assert "universe:topt-" not in definition
    assert "universe:canary-" in definition, "the multi-universe view excludes canary heads"
    assert "limit 1" not in definition.lower()


def test_ledger_column_order_matches_production(fresh_db: str) -> None:
    with psycopg.connect(fresh_db) as conn:
        columns = tuple(
            row[0]
            for row in conn.execute(
                "select attname from pg_attribute where attrelid = 'staging.api_call_ledger'::regclass "
                "and attnum > 0 and not attisdropped order by attnum"
            )
        )
    assert columns == PRODUCTION_LEDGER_COLUMNS


def test_no_code_names_a_retired_view() -> None:
    pattern = re.compile(r"\b(entity_graph_nodes|entity_graph_edges|entity_alias_resolution)\b")
    exempt = {Path(__file__).resolve(), RETIRE_MIGRATION.resolve()}
    offenders: list[str] = []
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", REPO_ROOT / "db"):
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".sql", ".ts", ".tsx"} or "node_modules" in path.parts:
                continue
            if path.resolve() in exempt:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == [], f"retired entity graph views still named in: {offenders}"
