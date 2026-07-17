"""Integration tests against a real local Postgres (skip gracefully without one).

These tests commit real, obviously-fake rows into `mart.strategy_runs`/
`strategy_decisions` (each tagged `test-strategy-run-postgres-<uuid>` or a
random `strategy-run:<uuid>...` ID) rather than rolling back, because
PostgresStrategyRunRepository opens its own connection per call and would
not see uncommitted rows from a test's own transaction. The tables are
append-only by design (real evidence is never deleted), so repeated local
runs accumulate a handful of harmless disposable rows over time; CI is
unaffected since it runs against an ephemeral Postgres container per run.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository

_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"
_HASH64 = "c" * 64


def _unique_strategy_key() -> str:
    # PostgresStrategyRunRepository opens its own connection per call (a
    # separate transaction from whatever inserted the test fixture rows), so
    # rows must be committed to be visible -- and mart.strategy_runs/
    # strategy_decisions are append-only (no DELETE for cleanup). A unique,
    # clearly-tagged strategy_key per test avoids collisions without needing
    # cleanup; the rows are harmless, obviously-fake, disposable local-dev
    # residue, never read by anything outside this test file.
    return f"test-strategy-run-postgres-{uuid.uuid4().hex}"


def _context() -> AccessContext:
    now = datetime.now(UTC)
    return AccessContext(
        context_id="ctx:test",
        principal_id="principal:test",
        tenant_id="tenant:test",
        session_id="session:test",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(_DATABASE_URL, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.close()


def _insert_run(connection, run_id: str, strategy_key: str, *, executed_at: datetime) -> None:
    connection.execute(
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, _HASH64, strategy_key, "v0", _HASH64, _HASH64, "preview", executed_at),
    )


def _insert_decision(connection, decision_id: str, run_id: str, *, issuer_id: str, cutoff_at: datetime) -> None:
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            _HASH64,
            run_id,
            issuer_id,
            cutoff_at,
            "272539.18",
            ValuationTier.LARGE_MODEL_NATIVE.value,
            "12.5",
            "18.75",
            "0.5",
            True,
            "selected",
            None,
            1,
            "1.0",
        ),
    )


def test_returns_unavailable_when_no_runs_recorded(connection) -> None:
    repository = PostgresStrategyRunRepository(database_url=_DATABASE_URL)
    result = repository.get_latest(strategy_id=_unique_strategy_key(), context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "no_runs_recorded"


def test_returns_the_latest_run_with_real_decisions(connection) -> None:
    # StrategyRunReport.strategy_id is a narrow Literal["large_model_value_v0"]
    # -- the only strategy the DTO admits today -- so, unlike the other tests
    # here, this one cannot use a disposable per-test strategy_key. Isolation
    # instead comes from `executed_at=now()` always outranking any prior
    # run's timestamp, so this test's row is deterministically "the latest"
    # regardless of what earlier local runs already committed.
    strategy_key = "large_model_value_v0"
    older = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    newer = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, older, strategy_key, executed_at=datetime(2020, 1, 1, tzinfo=UTC))
    _insert_run(connection, newer, strategy_key, executed_at=datetime.now(UTC))
    _insert_decision(
        connection,
        "strategy-decision:" + uuid.uuid4().hex + "0" * 32,
        newer,
        issuer_id="issuer:adm",
        cutoff_at=datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC),
    )

    repository = PostgresStrategyRunRepository(database_url=_DATABASE_URL)
    result = repository.get_latest(strategy_id=strategy_key, context=_context())

    assert isinstance(result, StrategyRunReport)
    assert result.source == "mart"
    assert result.corpus_sha256 == _HASH64
    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.issuer_id == "issuer:adm"
    assert decision.outcome.value == "selected"
    assert decision.tier == ValuationTier.LARGE_MODEL_NATIVE
    assert str(decision.valuation_gap) == "0.5"
    assert decision.confidence is None  # #355's schema has no confidence column yet


def test_ignores_runs_for_a_different_strategy_key(connection) -> None:
    requested_key = _unique_strategy_key()
    other_key = _unique_strategy_key()
    other_run = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, other_run, other_key, executed_at=datetime(2026, 1, 1, tzinfo=UTC))

    repository = PostgresStrategyRunRepository(database_url=_DATABASE_URL)
    result = repository.get_latest(strategy_id=requested_key, context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "no_runs_recorded"


def test_fails_closed_on_database_unavailable() -> None:
    repository = PostgresStrategyRunRepository(database_url="postgresql://nope:nope@127.0.0.1:1/does_not_exist")
    result = repository.get_latest(strategy_id="whatever", context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "database_unavailable"
