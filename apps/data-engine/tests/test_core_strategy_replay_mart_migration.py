import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings

_RUN_ID = "strategy-run:" + "a" * 64
_DECISION_ID = "strategy-decision:" + "d" * 64
_HASH64 = "b" * 64


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _insert_run(connection, run_id: str = _RUN_ID) -> None:
    connection.execute(
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, _HASH64, "large_model_value_v0", "v0", _HASH64, _HASH64, "preview", datetime.now(UTC)),
    )


def test_mart_strategy_tables_and_triggers_exist(connection) -> None:
    assert connection.execute(
        "select to_regclass('mart.strategy_runs'), to_regclass('mart.strategy_decisions')"
    ).fetchone() == ("mart.strategy_runs", "mart.strategy_decisions")
    assert connection.execute(
        """
        select count(*)
        from pg_trigger
        where not tgisinternal
          and tgname in ('trg_strategy_runs_append_only', 'trg_strategy_decisions_append_only')
        """
    ).fetchone() == (2,)


def test_strategy_runs_and_decisions_accept_a_valid_row(connection) -> None:
    _insert_run(connection)
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            eligible, outcome
        ) values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (_DECISION_ID, _HASH64, _RUN_ID, "issuer.ddog", datetime.now(UTC), True, "selected"),
    )
    row = connection.execute(
        "select issuer_id, outcome from mart.strategy_decisions where strategy_decision_id = %s", (_DECISION_ID,)
    ).fetchone()
    assert row == ("issuer.ddog", "selected")


def test_strategy_runs_are_append_only(connection) -> None:
    _insert_run(connection)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update mart.strategy_runs set claim_ceiling = 'tampered' where strategy_run_id = %s", (_RUN_ID,)
        )


def test_strategy_decisions_are_append_only(connection) -> None:
    _insert_run(connection)
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            eligible, outcome
        ) values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (_DECISION_ID, _HASH64, _RUN_ID, "issuer.ddog", datetime.now(UTC), True, "selected"),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update mart.strategy_decisions set outcome = 'tampered' where strategy_decision_id = %s", (_DECISION_ID,)
        )


def test_decision_requires_an_existing_run(connection) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            """
            insert into mart.strategy_decisions (
                strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
                eligible, outcome
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (_DECISION_ID, _HASH64, "strategy-run:" + "9" * 64, "issuer.ddog", datetime.now(UTC), True, "selected"),
        )


def test_decision_rejects_duplicate_run_issuer_cutoff(connection) -> None:
    _insert_run(connection)
    cutoff = datetime.now(UTC)
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            eligible, outcome
        ) values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (_DECISION_ID, _HASH64, _RUN_ID, "issuer.ddog", cutoff, True, "selected"),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        connection.execute(
            """
            insert into mart.strategy_decisions (
                strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
                eligible, outcome
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            ("strategy-decision:" + "f" * 64, _HASH64, _RUN_ID, "issuer.ddog", cutoff, True, "selected"),
        )
