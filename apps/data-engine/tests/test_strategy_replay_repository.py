import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.core_strategy_replay import run
from data_engine.strategy_replay_repository import (
    CLAIM_CEILING,
    write_replay,
    write_strategy_decision,
    write_strategy_run,
)
from truealpha_contracts.common import canonical_sha256

_EXECUTED_AT = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


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


def test_write_replay_persists_the_run_and_all_ten_decisions(connection) -> None:
    decisions, definition = run()

    run_id, decision_ids = write_replay(connection, decisions, definition, executed_at=_EXECUTED_AT)

    assert run_id.startswith("strategy-run:")
    assert len(decision_ids) == 10
    assert len(set(decision_ids)) == 10

    row = connection.execute(
        "select strategy_key, strategy_version, claim_ceiling from mart.strategy_runs where strategy_run_id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("large_model_value_v0", definition.definition_version, CLAIM_CEILING)

    count = connection.execute(
        "select count(*) from mart.strategy_decisions where strategy_run_id = %s", (run_id,)
    ).fetchone()[0]
    assert count == 10


def test_replaying_the_same_run_is_idempotent(connection) -> None:
    decisions, definition = run()

    first_run_id, first_decision_ids = write_replay(connection, decisions, definition, executed_at=_EXECUTED_AT)
    second_run_id, second_decision_ids = write_replay(connection, decisions, definition, executed_at=_EXECUTED_AT)

    assert first_run_id == second_run_id
    assert first_decision_ids == second_decision_ids
    count = connection.execute(
        "select count(*) from mart.strategy_decisions where strategy_run_id = %s", (first_run_id,)
    ).fetchone()[0]
    assert count == 10


def test_a_different_executed_at_is_a_new_run(connection) -> None:
    decisions, definition = run()

    first_run_id, _ = write_replay(connection, decisions, definition, executed_at=_EXECUTED_AT)
    second_run_id, _ = write_replay(connection, decisions, definition, executed_at=_EXECUTED_AT.replace(hour=13))

    assert first_run_id != second_run_id
    count = connection.execute("select count(*) from mart.strategy_runs").fetchone()[0]
    assert count == 2


def test_jpm_decision_persists_the_financial_branch_outcome(connection) -> None:
    decisions, definition = run()
    run_id = write_strategy_run(connection, definition, executed_at=_EXECUTED_AT)
    jpm = next(d for d in decisions if d.issuer_id == "issuer:jpm")

    write_strategy_decision(connection, jpm, strategy_run_id=run_id)

    row = connection.execute(
        "select capital_adjusted_labor_efficiency, exclusion_reason, eligible "
        "from mart.strategy_decisions where strategy_run_id = %s and issuer_id = %s",
        (run_id, "issuer:jpm"),
    ).fetchone()
    assert row is not None
    efficiency, exclusion_reason, eligible = row
    assert efficiency is not None
    assert exclusion_reason == "financial_valuation_not_comparable"
    assert eligible is False


def test_a_forged_row_under_the_computed_id_raises_on_replay(connection) -> None:
    """If some other writer already landed a row under the exact id this
    function would compute, but with different stored content (a corrupted
    or malicious insert bypassing this module), replaying the real decision
    must raise rather than silently trusting the mismatched row."""

    decisions, definition = run()
    run_id = write_strategy_run(connection, definition, executed_at=_EXECUTED_AT)
    jpm = next(d for d in decisions if d.issuer_id == "issuer:jpm")

    payload = {"strategy_run_id": run_id, **jpm.to_json()}
    forged_id = f"strategy-decision:{canonical_sha256(payload)}"
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at, eligible, outcome
        ) values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (forged_id, "f" * 64, run_id, "issuer:someone-else", jpm.cutoff_at, True, "selected"),
    )

    with pytest.raises(ValueError, match="identity conflict"):
        write_strategy_decision(connection, jpm, strategy_run_id=run_id)
