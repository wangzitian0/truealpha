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


def test_no_runtime_source_references_retired_tables() -> None:
    """Confirm apps/*/src and common/*/src do not read or write any retired table."""
    offenders: list[str] = []
    scanned = 0
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common"):
        for src_dir in sorted(root.glob("*/src")):
            for path in sorted(src_dir.rglob("*")):
                if path.suffix not in {".py", ".ts", ".tsx", ".sql"} or not path.is_file():
                    continue
                scanned += 1
                text = path.read_text(encoding="utf-8", errors="ignore")
                for table_name in RETIRED_TABLE_NAMES:
                    if table_name in text:
                        offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{table_name}")
    assert scanned > 0, "expected runtime source files under apps/*/src and common/*/src"
    assert offenders == [], f"retired tables referenced in runtime sources: {offenders}"


def test_fresh_migration_validates_all_constraints_and_retires_download_tickets() -> None:
    """Confirm fresh baseline leaves zero unvalidated constraints and zero download ticket objects."""
    with isolated_test_database("cleanup_step1", template=None) as db_url:
        with psycopg.connect(db_url) as conn:
            (unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert unvalidated == 0, f"expected 0 unvalidated constraints, found {unvalidated}"
            assert conn.execute("select to_regclass('app.research_document_download_tickets')").fetchone() == (None,)
            assert conn.execute("select to_regprocedure('app.reject_document_ticket_field_tamper()')").fetchone() == (
                None,
            )


def test_upgrade_validates_preexisting_not_valid_constraints_and_drops_download_tickets() -> None:
    """Confirm replay validates preexisting NOT VALID constraints and drops legacy ticket objects."""
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
                "create function app.reject_document_ticket_field_tamper() returns trigger language plpgsql as "
                "$$ begin return new; end; $$; "
                "create trigger trg_research_document_download_tickets_guard_update before update on "
                "app.research_document_download_tickets for each row execute function "
                "app.reject_document_ticket_field_tamper();"
            )
            (before_unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert before_unvalidated == 2

        apply_migration_chain(db_url)
        apply_migration_chain(db_url)

        with psycopg.connect(db_url) as conn:
            (after_unvalidated,) = conn.execute("select count(*) from pg_constraint where not convalidated").fetchone()
            assert after_unvalidated == 0
            assert conn.execute("select to_regclass('app.research_document_download_tickets')").fetchone() == (None,)
            assert conn.execute("select to_regprocedure('app.reject_document_ticket_field_tamper()')").fetchone() == (
                None,
            )
