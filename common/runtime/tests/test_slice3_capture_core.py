"""Slice 3 gate: retire `datahub/production_topt` and universe-specific `topt_*` capture objects (#1730)."""

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
LEGACY_PACKAGE_DIR = REPO_ROOT / "apps/data-engine/src/data_engine/datahub/production_topt"
LEGACY_TEST_DIR = REPO_ROOT / "apps/data-engine/tests/production_topt"
CONVERGED_PACKAGE_DIR = REPO_ROOT / "apps/data-engine/src/data_engine/datahub/capture_core"

LEGACY_IDENTIFIERS_RE = re.compile(
    r"\b("
    r"datahub\.production_topt"
    r"|production_topt_run_plans"
    r"|topt_core_snapshots"
    r"|topt_core_snapshot_members"
    r"|topt_capture_status"
    r"|topt_capture_meta_info"
    r"|validate_production_topt_run_plan"
    r"|validate_topt_core_snapshot"
    r"|validate_topt_core_snapshot_member"
    r")\b"
)


def _admin_url() -> str:
    params = conninfo_to_dict(guard_db_write())
    return make_conninfo(**(params | {"dbname": "postgres"}))


@pytest.fixture(scope="module")
def slice3_db() -> Generator[str]:
    admin_url = _admin_url()
    name = f"truealpha_slice3_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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


def test_legacy_production_topt_directories_and_code_references_retired() -> None:
    assert not LEGACY_PACKAGE_DIR.exists(), f"{LEGACY_PACKAGE_DIR} must be deleted after Slice 3 move"
    assert not LEGACY_TEST_DIR.exists(), f"{LEGACY_TEST_DIR} must move to tests/capture_core"
    assert CONVERGED_PACKAGE_DIR.is_dir(), f"{CONVERGED_PACKAGE_DIR} must exist"

    exempt_files = {
        Path(__file__).resolve(),
        (REPO_ROOT / "common/runtime/tests/test_migration_applier.py").resolve(),
        (REPO_ROOT / "common/runtime/tests/test_migration_boot_locks.py").resolve(),
        (REPO_ROOT / "common/contracts/tests/fixtures/contract_cases.json").resolve(),
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
            for match in LEGACY_IDENTIFIERS_RE.finditer(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)}")
    assert not offenders, "legacy Slice 3 identifiers remain:\n" + "\n".join(offenders[:30])


def test_slice3_relations_and_triggers_exist_without_legacy_copies(slice3_db: str) -> None:
    with psycopg.connect(slice3_db) as conn:
        for legacy in (
            "raw.production_topt_run_plans",
            "staging.topt_core_snapshots",
            "staging.topt_core_snapshot_members",
            "mart.topt_capture_status",
            "mart.topt_capture_meta_info",
        ):
            assert conn.execute("select to_regclass(%s)", (legacy,)).fetchone() == (None,), legacy

        for target in (
            "raw.capture_run_plans",
            "staging.core_snapshots",
            "staging.core_snapshot_members",
            "mart.capture_status",
            "mart.capture_meta_info",
            "mart.data_engine_identity",
            "mart.entity_display_resolution",
        ):
            assert conn.execute("select to_regclass(%s)", (target,)).fetchone()[0] is not None, target

        for legacy_fn in (
            "raw.validate_production_topt_run_plan()",
            "staging.validate_topt_core_snapshot()",
            "staging.validate_topt_core_snapshot_member()",
        ):
            assert conn.execute("select to_regprocedure(%s)", (legacy_fn,)).fetchone() == (None,), legacy_fn

        for target_fn in (
            "raw.validate_capture_run_plan()",
            "staging.validate_core_snapshot()",
            "staging.validate_core_snapshot_member()",
        ):
            assert conn.execute("select to_regprocedure(%s)", (target_fn,)).fetchone()[0] is not None, target_fn


def test_slice3_migration_renames_pre_slice3_database_cleanly(slice3_db: str) -> None:
    from truealpha_tools.schema_drift import differences, fingerprint

    slice3_migrations = sorted(
        (REPO_ROOT / "db" / "migrations").glob("*_datahub_slice3_retire_production_topt_naming.sql")
    )
    assert slice3_migrations, "missing Slice 3 timestamped migration file"

    admin_url = _admin_url()
    name = f"truealpha_slice3_upgrade_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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
            # Revert the 5 Slice 3 objects back to their pre-Slice-3 names, views, and validators.
            conn.execute(
                """
                alter table raw.capture_run_plans rename to production_topt_run_plans;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_pkey to production_topt_run_plans_pkey;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_content_sha256_key to production_topt_run_plans_content_sha256_key;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_content_sha256_check to production_topt_run_plans_content_sha256_check;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_payload_check to production_topt_run_plans_payload_check;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_release_manifest_id_check to production_topt_run_plans_release_manifest_id_check;
                alter table raw.production_topt_run_plans rename constraint capture_run_plans_run_id_fkey to production_topt_run_plans_run_id_fkey;
                CREATE OR REPLACE FUNCTION raw.validate_production_topt_run_plan() RETURNS trigger LANGUAGE plpgsql AS $$
                begin return new; end; $$;
                drop trigger validate_plan on raw.production_topt_run_plans;
                create trigger validate_plan before insert on raw.production_topt_run_plans
                    for each row execute function raw.validate_production_topt_run_plan();
                drop function raw.validate_capture_run_plan();
                create or replace view mart.data_engine_identity as
                 SELECT production_topt_run_plans.run_id,
                    production_topt_run_plans.release_manifest_id,
                    COALESCE(production_topt_run_plans.payload ->> 'data_engine_git_sha'::text, 'unknown'::text) AS git_sha,
                    COALESCE(production_topt_run_plans.payload ->> 'data_engine_image_digest'::text, 'unknown'::text) AS image_digest,
                    production_topt_run_plans.created_at
                   FROM raw.production_topt_run_plans;

                alter view mart.capture_meta_info rename to topt_capture_meta_info;
                alter view mart.capture_status rename to topt_capture_status;
                alter table staging.core_snapshots rename to topt_core_snapshots;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_pkey to topt_core_snapshots_pkey;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_content_sha256_key to topt_core_snapshots_content_sha256_key;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_run_id_key to topt_core_snapshots_run_id_key;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_check to topt_core_snapshots_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_content_sha256_check to topt_core_snapshots_content_sha256_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_instrument_count_check to topt_core_snapshots_instrument_count_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_issuer_count_check to topt_core_snapshots_issuer_count_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_observation_count_check to topt_core_snapshots_observation_count_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_payload_check to topt_core_snapshots_payload_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_snapshot_id_check to topt_core_snapshots_snapshot_id_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_universe_sha256_check to topt_core_snapshots_universe_sha256_check;
                alter table staging.topt_core_snapshots rename constraint core_snapshots_run_id_fkey to topt_core_snapshots_run_id_fkey;
                alter table staging.core_snapshot_members rename to topt_core_snapshot_members;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_pkey to topt_core_snapshot_members_pkey;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_snapshot_id_instrument_id_key to topt_core_snapshot_members_snapshot_id_instrument_id_key;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_snapshot_id_listing_id_key to topt_core_snapshot_members_snapshot_id_listing_id_key;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_factor_input_check to topt_core_snapshot_members_factor_input_check;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_member_sha256_check to topt_core_snapshot_members_member_sha256_check;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_observation_ids_check to topt_core_snapshot_members_observation_ids_check;
                alter table staging.topt_core_snapshot_members rename constraint core_snapshot_members_snapshot_id_fkey to topt_core_snapshot_members_snapshot_id_fkey;
                CREATE OR REPLACE FUNCTION staging.validate_topt_core_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
                begin return new; end; $$;
                drop trigger validate_snapshot on staging.topt_core_snapshots;
                create trigger validate_snapshot before insert on staging.topt_core_snapshots
                    for each row execute function staging.validate_topt_core_snapshot();
                drop function staging.validate_core_snapshot();
                CREATE OR REPLACE FUNCTION staging.validate_topt_core_snapshot_member() RETURNS trigger LANGUAGE plpgsql AS $$
                begin return new; end; $$;
                drop trigger validate_member on staging.topt_core_snapshot_members;
                create trigger validate_member before insert on staging.topt_core_snapshot_members
                    for each row execute function staging.validate_topt_core_snapshot_member();
                drop function staging.validate_core_snapshot_member();
                """
            )
            # Replicate the exact live upgrade boot order (0001..0004 before timestamped migrations).
            apply_migration_chain(db_url)

            for old_name in (
                "raw.production_topt_run_plans",
                "staging.topt_core_snapshots",
                "staging.topt_core_snapshot_members",
                "mart.topt_capture_status",
                "mart.topt_capture_meta_info",
            ):
                assert conn.execute("select to_regclass(%s)", (old_name,)).fetchone() == (None,), old_name
            for new_name in (
                "raw.capture_run_plans",
                "staging.core_snapshots",
                "staging.core_snapshot_members",
                "mart.capture_status",
                "mart.capture_meta_info",
                "mart.data_engine_identity",
                "mart.entity_display_resolution",
            ):
                assert conn.execute("select to_regclass(%s)", (new_name,)).fetchone()[0] is not None, new_name
            for old_con in (
                "production_topt_run_plans_pkey",
                "topt_core_snapshots_pkey",
                "topt_core_snapshot_members_pkey",
            ):
                assert (
                    conn.execute("select count(*) from pg_constraint where conname = %s", (old_con,)).fetchone()[0] == 0
                ), old_con
            with psycopg.connect(slice3_db) as fresh:
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
