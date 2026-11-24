"""Guard invariants for DB and storage cleanup (#1852, #1815)."""

from __future__ import annotations

from pathlib import Path

import psycopg
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]
RETIRED_TABLE_NAMES = (
    "research_document_download_tickets",
    "capture_entity_refs",
    "strategy_input_coverage",
    "market_prices_monthly",
    "universe_mask",
)
RETIRED_QUALIFIED_TABLES = (
    "app.research_document_download_tickets",
    "staging.capture_entity_refs",
    "mart.strategy_input_coverage",
    "staging.market_prices_monthly",
    "staging.universe_mask",
)


def test_no_source_or_migration_references_retired_tables() -> None:
    """Confirm apps, common, and db do not use retired tables outside guarded SQL drops."""
    offenders: list[str] = []
    scanned = 0
    this_file = Path(__file__).resolve()
    migrations_dir = (REPO_ROOT / "db" / "migrations").resolve()
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", REPO_ROOT / "db"):
        for path in sorted(root.rglob("*")):
            if path.resolve() == this_file:
                continue
            if path.suffix not in {".py", ".ts", ".tsx", ".sql"} or not path.is_file():
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="ignore")
            is_migration = migrations_dir in path.resolve().parents
            for table_name in RETIRED_TABLE_NAMES:
                if table_name not in text:
                    continue
                if not is_migration:
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{table_name}")
                    continue
                lines = text.splitlines()
                for lineno, line in enumerate(lines, start=1):
                    if table_name not in line:
                        continue
                    stripped = line.strip().lower()
                    prev = lines[lineno - 2].strip().lower() if lineno >= 2 else ""
                    if stripped.startswith("if to_regclass(") or (
                        stripped.startswith("drop table ")
                        and stripped.endswith(";")
                        and prev.startswith("if to_regclass(")
                    ):
                        continue
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}:{table_name}")
    assert scanned > 0, "expected source files under apps, common, and db"
    assert offenders == [], f"retired tables referenced outside guarded drops: {offenders}"


def test_fresh_migration_validates_all_constraints_and_retires_dead_tables() -> None:
    """Confirm fresh baseline leaves zero unvalidated constraints and zero retired table objects."""
    with isolated_test_database("cleanup_step1", template=None) as db_url:
        with psycopg.connect(db_url) as conn:
            (unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert unvalidated == 0, f"expected 0 unvalidated constraints, found {unvalidated}"
            for qualified_name in RETIRED_QUALIFIED_TABLES:
                assert conn.execute("select to_regclass(%s)", (qualified_name,)).fetchone() == (None,), qualified_name
            assert conn.execute("select to_regprocedure('app.reject_document_ticket_field_tamper()')").fetchone() == (
                None,
            )


def test_upgrade_validates_preexisting_not_valid_constraints_and_drops_retired_tables() -> None:
    """Confirm replay validates preexisting NOT VALID constraints and drops legacy retired tables."""
    with isolated_test_database("cleanup_step1_upg", template=None) as db_url:
        with psycopg.connect(db_url, autocommit=True) as conn:
            for tbl, con, ddl in (
                (
                    "raw.capture_runs",
                    "capture_runs_schedule_policy_id_fkey",
                    "FOREIGN KEY (schedule_policy_id) REFERENCES raw.capture_schedule_policies(schedule_policy_id) NOT VALID",
                ),
                (
                    "mart.backtest_runs",
                    "backtest_runs_succeeded_declares_basis",
                    "CHECK (((status <> 'succeeded'::text) OR ((definition_content_sha256 IS NOT NULL) "
                    "AND (price_basis IS NOT NULL) AND (claim_ceiling IS NOT NULL) AND (benchmark_rule IS NOT NULL)))) NOT VALID",
                ),
            ):
                conn.execute(f"alter table {tbl} drop constraint {con}")
                conn.execute(f"alter table {tbl} add constraint {con} {ddl}")
            conn.execute(
                "create table app.research_document_download_tickets (ticket_id text primary key); "
                "insert into app.research_document_download_tickets values ('t-1'); "
                "create function app.reject_document_ticket_field_tamper() returns trigger language plpgsql as "
                "$$ begin return new; end; $$; "
                "create trigger trg_research_document_download_tickets_guard_update before update on "
                "app.research_document_download_tickets for each row execute function "
                "app.reject_document_ticket_field_tamper(); "
                "create table staging.capture_entity_refs (observation_id text not null, role text not null); "
                "insert into staging.capture_entity_refs values ('obs-1', 'issuer'); "
                "create trigger reject_mutation before delete or update on staging.capture_entity_refs "
                "for each row execute function staging.reject_entity_mutation(); "
                "create index idx_capture_entity_refs_entity on staging.capture_entity_refs (observation_id); "
                "create table staging.market_prices_monthly (id bigint generated always as identity primary key); "
                "insert into staging.market_prices_monthly default values; "
                "create trigger trg_market_prices_monthly_append_only before delete or update on "
                "staging.market_prices_monthly for each row execute function staging.reject_point_in_time_mutation(); "
                "create index idx_market_prices_monthly_symbol_date on staging.market_prices_monthly (id); "
                "create table staging.universe_mask (symbol text not null, cutoff_date date not null); "
                "insert into staging.universe_mask values ('AAPL', '2026-01-01'); "
                "create index idx_universe_mask_cutoff_eligible on staging.universe_mask (symbol); "
                "create table mart.strategy_input_coverage (run_id text not null, issuer_id text not null); "
                "insert into mart.strategy_input_coverage values ('run-1', 'iss-1'); "
                "create trigger reject_mutation before delete or update on mart.strategy_input_coverage "
                "for each row execute function mart.reject_mutation();"
            )
            (before_unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert before_unvalidated == 2
            for qualified_name in RETIRED_QUALIFIED_TABLES:
                assert conn.execute("select to_regclass(%s)", (qualified_name,)).fetchone() != (None,), qualified_name

        apply_migration_chain(db_url)
        apply_migration_chain(db_url)

        with psycopg.connect(db_url) as conn:
            (after_unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert after_unvalidated == 0
            for qualified_name in RETIRED_QUALIFIED_TABLES:
                assert conn.execute("select to_regclass(%s)", (qualified_name,)).fetchone() == (None,), qualified_name
            assert conn.execute("select to_regprocedure('app.reject_document_ticket_field_tamper()')").fetchone() == (
                None,
            )
