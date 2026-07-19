from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.core_strategy_replay import _compare_against_golden, _load_corpus
from data_engine.strategy_backtest_gateway import (
    StrategyBacktestGateway,
    run_backtest_from_staging,
    seed_strategy_backtest_inputs,
)
from data_engine.strategy_replay_repository import write_replay
from truealpha_contracts.strategy import LargeModelValueV0Definition

_EXECUTED_AT = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


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


def _definition() -> LargeModelValueV0Definition:
    corpus = _load_corpus()
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))


def test_e2e_strategy_runs_from_staging_through_gateway_to_mart(connection) -> None:
    # #395 end-to-end: capture the strategy's grounded inputs into staging, run the
    # single-source evaluator over the gateway (staging, not the fixture), reproduce
    # the #21 golden, and persist to the real mart with PIT snapshot lineage.
    corpus = _load_corpus()
    definition = _definition()

    expected_input_rows = sum(len(decision["inputs"]) for decision in corpus["golden_decision_set"]["decisions"])
    written = seed_strategy_backtest_inputs(connection, corpus)
    assert written == expected_input_rows

    decisions, snapshot_id = run_backtest_from_staging(connection, corpus, definition)

    # The strategy reproduces every golden decision exactly -- from staging, not the JSON.
    assert len(decisions) == 10
    assert _compare_against_golden(decisions, corpus) == []
    assert snapshot_id.startswith("strategy-snapshot:")

    run_id, decision_ids = write_replay(
        connection, decisions, definition, executed_at=_EXECUTED_AT, snapshot_id=snapshot_id
    )
    assert len(decision_ids) == 10

    # The persisted run binds the exact captured snapshot; re-reading proves the lineage.
    row = connection.execute(
        "select snapshot_id from mart.strategy_runs where strategy_run_id = %s", (run_id,)
    ).fetchone()
    assert row == (snapshot_id,)


def test_gateway_snapshot_id_is_content_addressed_on_captured_inputs(connection) -> None:
    corpus = _load_corpus()
    seed_strategy_backtest_inputs(connection, corpus)
    gateway = StrategyBacktestGateway(connection)
    cutoff = corpus["golden_decision_set"]["decisions"][0]["cutoff_at"]

    first = gateway.snapshot_id(cutoff)
    second = gateway.snapshot_id(cutoff)
    assert first == second == gateway.snapshot_id(cutoff)
    assert first.startswith("strategy-snapshot:")

    # Every issuer at the cutoff is loaded with its full input set.
    issuers = gateway.issuer_inputs(cutoff)
    assert {issuer.issuer_id for issuer in issuers} == {
        "issuer:adm",
        "issuer:ddog",
        "issuer:jpm",
        "issuer:nice",
        "issuer:shop",
    }
