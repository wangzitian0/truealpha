"""Guard invariants for DB and storage cleanup (#1852, #1815)."""

from __future__ import annotations

import psycopg
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database


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
