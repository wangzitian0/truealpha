"""The runtime login of llm-service and app-web starts each session with JIT off (#1707)."""

from __future__ import annotations

import psycopg


def test_the_migrated_runtime_login_disables_jit_while_the_server_default_keeps_it_on() -> None:
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("pg_jit") as db, psycopg.connect(str(db), options="") as conn:
        # The server default must be on, or this test could not see the role setting it guards.
        assert conn.execute("show jit").fetchone() == ("on",)
        (config,) = conn.execute("select rolconfig from pg_roles where rolname = 'app_service_login'").fetchone()
        assert "jit=off" in config, f"app_service_login role settings: {config}"
        assert "statement_timeout=5s" in config, f"app_service_login role settings: {config}"
