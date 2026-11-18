"""Slice 4 gate: retire legacy wide `mart.topt_gppe_*` / `mart.topt_core_*` relations (#1730)."""

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

RETIRED_RELATIONS = (
    "mart.topt_gppe_invocations",
    "mart.topt_gppe_results",
    "mart.topt_core_invocations",
    "mart.topt_core_results",
    "mart.topt_core_meta_info",
    "mart.topt_core_result_read",
)
RETIRED_FUNCTIONS = (
    "mart.validate_topt_core_result()",
    "mart.validate_topt_gppe_result()",
)
RETIRED_IDENTIFIERS_RE = re.compile(
    r"\b("
    r"topt_gppe_invocations"
    r"|topt_gppe_results"
    r"|topt_core_invocations"
    r"|topt_core_results"
    r"|topt_core_meta_info"
    r"|topt_core_result_read"
    r"|validate_topt_core_result"
    r"|validate_topt_gppe_result"
    r")\b"
)


def _admin_url() -> str:
    params = conninfo_to_dict(guard_db_write())
    return make_conninfo(**(params | {"dbname": "postgres"}))


@pytest.fixture(scope="module")
def slice4_db() -> Generator[str]:
    admin_url = _admin_url()
    name = f"truealpha_slice4_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs required DB tests")

    params = conninfo_to_dict(admin_url)
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        apply_migration_chain(db_url)
        yield db_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def _docstring_expr_spans(source: str) -> set[tuple[int, int]]:
    import ast

    tree = ast.parse(source)
    spans: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.add((first.lineno, first.end_lineno or first.lineno))
    return spans


def test_retired_topt_mart_identifiers_removed_from_runtime_code_and_schema() -> None:
    exempt_files = {
        Path(__file__).resolve(),
        (REPO_ROOT / "common/runtime/tests/test_served_head_guard.py").resolve(),
        (REPO_ROOT / "common/runtime/tests/test_migration_applier.py").resolve(),
        (REPO_ROOT / "common/runtime/tests/test_migration_boot_locks.py").resolve(),
    }
    offenders: list[str] = []
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", REPO_ROOT / "db"):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sql", ".ts", ".tsx"}:
                continue
            resolved = path.resolve()
            if resolved in exempt_files:
                continue
            if resolved.parent == (REPO_ROOT / "db/migrations").resolve() and path.name.startswith("2026"):
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".py" and "/tests/" in path.as_posix():
                doc_spans = _docstring_expr_spans(text)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if line.lstrip().startswith("#") or any(s <= lineno <= e for s, e in doc_spans):
                        continue
                    for match in RETIRED_IDENTIFIERS_RE.finditer(line):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(1)}")
                continue
            for match in RETIRED_IDENTIFIERS_RE.finditer(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)}")
    assert not offenders, "retired Slice 4 identifiers remain:\n" + "\n".join(offenders[:30])


def test_slice4_schema_has_lineage_view_and_no_retired_topt_mart_objects(slice4_db: str) -> None:
    with psycopg.connect(slice4_db) as conn:
        for retired in RETIRED_RELATIONS:
            assert conn.execute("select to_regclass(%s)", (retired,)).fetchone() == (None,), retired
        for retired_fn in RETIRED_FUNCTIONS:
            assert conn.execute("select to_regprocedure(%s)", (retired_fn,)).fetchone() == (None,), retired_fn
        assert conn.execute("select to_regclass('mart.capture_issuer_lineage')").fetchone()[0] is not None
        conn.execute("set local role mart_readonly")
        assert conn.execute("select count(*) from mart.capture_issuer_lineage").fetchone() == (0,)


def test_lineage_view_joins_the_banking_node_for_financial_issuers(slice4_db: str) -> None:
    with psycopg.connect(slice4_db) as conn:
        definition = conn.execute("select pg_get_viewdef(to_regclass('mart.capture_issuer_lineage'), true)").fetchone()[
            0
        ]
    assert "gppe_banking_tce_v1" in definition
    assert "gppe_uniform_charge_v0" in definition


def test_slice4_migration_backfills_and_retires_pre_slice4_database(slice4_db: str) -> None:
    from truealpha_tools.schema_drift import differences, fingerprint

    admin_url = _admin_url()
    name = f"truealpha_slice4_upgrade_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip(f"no local database: {error}")

    params = conninfo_to_dict(admin_url)
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute((REPO_ROOT / "db/migrations/0001_raw.sql").read_text())
            conn.execute((REPO_ROOT / "db/migrations/0002_staging.sql").read_text())
            conn.execute((REPO_ROOT / "db/migrations/0003_mart.sql").read_text())
            seed_sha = "a" * 64
            # Recreate the pre-Slice-4 wide tables, pre-Slice-4 status check, and seeded rows
            # (both missing legacy NULL-status rows and a dual-written not_applicable parity row).
            # Separate DDL from parameterized DML because psycopg forbids params on multi-statement queries.
            conn.execute(
                """
                alter table mart.metric_node_values drop constraint metric_node_values_availability_status_check;
                alter table mart.metric_node_values
                    add constraint metric_node_values_availability_status_check
                    check (availability_status = any (array[
                        'available'::text, 'unavailable'::text, 'stale'::text,
                        'excluded'::text, 'low_confidence'::text, 'error'::text
                    ]));
                create or replace function mart.validate_topt_gppe_result() returns trigger language plpgsql as $$
                begin return new; end; $$;
                create or replace function mart.validate_topt_core_result() returns trigger language plpgsql as $$
                begin return new; end; $$;
                create table mart.topt_gppe_invocations (
                    invocation_id text primary key,
                    snapshot_id text not null,
                    gppe_definition_id text not null
                );
                create table mart.topt_gppe_results (
                    result_id text primary key,
                    invocation_id text references mart.topt_gppe_invocations(invocation_id),
                    snapshot_id text not null default 'snap:1',
                    run_id text not null,
                    cutoff timestamptz not null,
                    issuer_id text not null,
                    availability text not null,
                    capital_adjusted_gross_profit numeric,
                    gppe numeric,
                    confidence numeric not null,
                    freshness text not null,
                    reason_codes text[] not null default '{}',
                    input_observation_ids text[] not null default '{}',
                    gppe_definition_sha256 text not null,
                    availability_status text,
                    source_evidence_status text,
                    factor_validation_status text
                );
                create table mart.topt_core_invocations (
                    invocation_id text primary key,
                    gppe_invocation_id text references mart.topt_gppe_invocations(invocation_id)
                );
                create table mart.topt_core_results (
                    result_id text primary key,
                    invocation_id text references mart.topt_core_invocations(invocation_id),
                    snapshot_id text not null default 'snap:1',
                    gppe_invocation_id text not null default 'inv:1',
                    gppe_result_id text references mart.topt_gppe_results(result_id),
                    operating_period_end date
                );
                create view mart.topt_core_result_read as select result_id from mart.topt_core_results;
                create view mart.topt_core_meta_info as select result_id from mart.topt_core_results;
                """
            )
            conn.execute("insert into mart.topt_gppe_invocations values ('inv:1', 'snap:1', 'def:1')")
            conn.execute(
                """
                insert into mart.topt_gppe_results values
                    ('res:legacy', 'inv:1', 'snap:1', 'capture-run:legacy', '2026-04-01T00:00:00Z',
                     'issuer:legacy', 'available', 2000, 20, 0.9, 'fresh', '{}', array['o1'], %s,
                     null, null, null),
                    ('res:exempt', 'inv:1', 'snap:1', 'capture-run:exempt', '2026-04-02T00:00:00Z',
                     'issuer:exempt', 'unavailable', null, null, 0, 'fresh', array['missing_gross_profit'], array['o2'], %s,
                     'not_applicable', 'verified', 'not_evaluated')
                """,
                (seed_sha, seed_sha),
            )
            conn.execute("insert into mart.topt_core_invocations values ('cinv:1', 'inv:1')")
            conn.execute(
                "insert into mart.topt_core_results values ('cres:legacy', 'cinv:1', 'snap:1', 'inv:1', 'res:legacy', '2025-12-31')"
            )
            conn.execute(
                """
                insert into mart.metric_node_values (
                    run_id, issuer_id, node_id, node_key, period, period_tag,
                    value, unit, availability_status, reason_codes,
                    source_evidence_status, factor_validation_status,
                    confidence, tree_id, tree_version, tree_sha256, input_observation_ids
                ) values (
                    'capture-run:exempt', 'issuer:exempt', '6143cf17-ceba-5f78-a2d3-a7cc423c49a7'::uuid,
                    'gppe_uniform_charge_v0', '2025-12-31', 'FY', null, 'per_employee',
                    'unavailable', array['missing_gross_profit'], 'verified', 'not_evaluated', 0,
                    'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid, 'production-topt-v0.2.0', %s, array['o2']
                )
                """,
                (seed_sha,),
            )
            apply_migration_chain(db_url)

            for retired in RETIRED_RELATIONS:
                assert conn.execute("select to_regclass(%s)", (retired,)).fetchone() == (None,), retired
            for retired_fn in RETIRED_FUNCTIONS:
                assert conn.execute("select to_regprocedure(%s)", (retired_fn,)).fetchone() == (None,), retired_fn

            rows = conn.execute(
                """
                select run_id, issuer_id, node_key, period, value, availability_status,
                       source_evidence_status, factor_validation_status
                from mart.metric_node_values
                order by run_id, issuer_id, node_key
                """
            ).fetchall()
            assert rows == [
                (
                    "capture-run:exempt",
                    "issuer:exempt",
                    "capital_adjusted_gross_profit",
                    "2026-04-02",
                    None,
                    "not_applicable",
                    "verified",
                    "not_evaluated",
                ),
                (
                    "capture-run:exempt",
                    "issuer:exempt",
                    "gppe_uniform_charge_v0",
                    "2025-12-31",
                    None,
                    "not_applicable",
                    "verified",
                    "not_evaluated",
                ),
                (
                    "capture-run:legacy",
                    "issuer:legacy",
                    "capital_adjusted_gross_profit",
                    "2025-12-31",
                    2000,
                    "available",
                    "degraded",
                    "not_evaluated",
                ),
                (
                    "capture-run:legacy",
                    "issuer:legacy",
                    "gppe_uniform_charge_v0",
                    "2025-12-31",
                    20,
                    "available",
                    "degraded",
                    "not_evaluated",
                ),
            ]
            with psycopg.connect(slice4_db) as fresh:
                assert (
                    differences(
                        fingerprint(conn, ["raw", "staging", "mart"]),
                        fingerprint(fresh, ["raw", "staging", "mart"]),
                    )
                    == []
                )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
