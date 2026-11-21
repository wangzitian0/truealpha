"""Each Postgres connection of llm-service starts with JIT off (#1707)."""

from __future__ import annotations

import psycopg


def test_every_service_connection_runs_with_jit_off_while_the_server_default_is_on() -> None:
    import llm_service
    from llm_service.services._db import with_mart_readonly
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("pg_jit") as db:
        # An explicit empty `options` ignores PGOPTIONS: this is the server default, which must be on,
        # or this test could not see the setting it guards.
        with psycopg.connect(str(db), options="") as raw:
            assert raw.execute("show jit").fetchone() == ("on",)

        assert with_mart_readonly(lambda cur: cur.execute("show jit").fetchone(), database_url=str(db)) == {
            "jit": "off"
        }

        # A truealpha_runtime reader opens its own connection with no options; PGOPTIONS still applies.
        with psycopg.connect(str(db)) as runtime_style:
            assert runtime_style.execute("show jit").fetchone() == ("off",)

    assert llm_service.JIT_OFF_OPTION == "-c jit=off"
