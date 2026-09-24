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
        "select snapshot_id, corpus_sha256 from mart.strategy_runs where strategy_run_id = %s", (run_id,)
    ).fetchone()
    assert row == (snapshot_id, snapshot_id.split(":", 1)[1])


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


def test_mutating_input_fact_changes_snapshot_and_corpus_sha256(connection) -> None:
    """#955: mutating an input fact produces a different snapshot_id and corpus_sha256."""
    corpus = _load_corpus()
    seed_strategy_backtest_inputs(connection, corpus)
    gateway = StrategyBacktestGateway(connection)
    cutoff = corpus["golden_decision_set"]["decisions"][0]["cutoff_at"]

    original_snapshot = gateway.snapshot_id(cutoff)
    original_corpus_sha = original_snapshot.split(":", 1)[1]

    # Mutate one fact in staging.strategy_backtest_inputs for this exact cutoff
    connection.execute(
        """
        update staging.strategy_backtest_inputs
        set value = value + 100
        where ctid in (select ctid from staging.strategy_backtest_inputs where cutoff_at = %s limit 1)
        """,
        (cutoff,),
    )
    mutated_snapshot = gateway.snapshot_id(cutoff)
    mutated_corpus_sha = mutated_snapshot.split(":", 1)[1]

    assert mutated_snapshot != original_snapshot
    assert mutated_corpus_sha != original_corpus_sha


def test_rows_for_cutoff_supersedes_by_knowable_at_not_insertion_order(connection) -> None:
    """#530: `_rows_for_cutoff` used to break ties on `recorded_at` (ingestion audit
    time) -- init.md §6 says explicitly "as-of resolution never reads it", and this IS
    an as-of resolution (`where cutoff_at = %s`). Construct the exact shape that made
    the old rule wrong: a row with an EARLIER `knowable_at` inserted SECOND (later
    `recorded_at`) must not outrank a row with a LATER `knowable_at` inserted first.

    Reverse-verified: reverting the ORDER BY to `recorded_at desc` (dropping
    `knowable_at desc`) makes this test select 999 instead of 100 -- confirmed by hand
    before landing this test, per this repo's rule 7."""
    issuer_id = "issuer:t530-tiebreak"
    cutoff_at = datetime(2026, 7, 1, tzinfo=UTC)

    # Inserted FIRST (earlier recorded_at), but the LATER knowable_at -- must win.
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, recorded_at)
        values (%s, %s, 'revenue', 100, 0.9, %s, %s)
        """,
        (issuer_id, cutoff_at, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC)),
    )
    # Inserted SECOND (later recorded_at), but the EARLIER knowable_at -- must lose,
    # even though it was written to the table more recently.
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, recorded_at)
        values (%s, %s, 'revenue', 999, 0.9, %s, %s)
        """,
        (issuer_id, cutoff_at, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 3, tzinfo=UTC)),
    )

    gateway = StrategyBacktestGateway(connection)
    rows = gateway._rows_for_cutoff(cutoff_at)
    matching = [row for row in rows if row[0] == issuer_id]
    assert len(matching) == 1, f"expected one resolved row for this (issuer, input_key), got {matching}"
    assert matching[0][2] == 100, (
        f"expected the row with the LATER knowable_at (value 100) to win, got {matching[0]} -- "
        "a later-recorded but earlier-knowable row must never outrank it"
    )
