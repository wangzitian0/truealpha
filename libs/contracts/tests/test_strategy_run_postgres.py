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

_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"
_HASH64 = "c" * 64


def _resolve_database_url() -> str:
    """Match whatever `DATABASE_URL` the environment configures (CI sets its
    own; a developer's local override should not be silently ignored) rather
    than a value hard-coded independently of it -- otherwise the fixture's
    connectivity check and the repository under test can end up pointed at
    two different databases."""
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


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
        active = psycopg.connect(_resolve_database_url(), connect_timeout=3, autocommit=True)
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
    repository = PostgresStrategyRunRepository(database_url=_resolve_database_url())
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

    repository = PostgresStrategyRunRepository(database_url=_resolve_database_url())
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


def test_decisions_are_ordered_by_cutoff_then_issuer_matching_the_fixture(connection) -> None:
    """The fixture's committed payload orders decisions by cutoff_at, then
    issuer_id (verified against `strategy_run_preview.v1.json`); the two
    backends implementing the same protocol must agree (#41's cross-backend
    consistency principle), regardless of insertion order.

    Uses the real "large_model_value_v0" strategy_key -- like
    test_returns_the_latest_run_with_real_decisions, StrategyRunReport's
    Literal strategy_id rules out a disposable per-test key -- and relies on
    `executed_at=now()` to deterministically outrank whatever earlier local
    runs already committed under the same key.
    """
    strategy_key = "large_model_value_v0"
    run_id = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, run_id, strategy_key, executed_at=datetime.now(UTC))
    early = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)
    late = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    # issuer_id is chosen so the two candidate orderings genuinely disagree:
    # sorting by issuer_id first would put "issuer:aaa" (late) before
    # "issuer:mmm"/"issuer:zzz" (early) -- if it instead used alphabetically-
    # matching names for early/late this test would pass even with the wrong
    # ORDER BY, which is exactly the mistake the first version of this test
    # made (verified by temporarily reverting the fix: it passed regardless).
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:aaa", cutoff_at=late
    )
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:zzz", cutoff_at=early
    )
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:mmm", cutoff_at=early
    )

    repository = PostgresStrategyRunRepository(database_url=_resolve_database_url())
    result = repository.get_latest(strategy_id=strategy_key, context=_context())

    assert isinstance(result, StrategyRunReport)
    ordered = [(d.cutoff_at, d.issuer_id) for d in result.decisions]
    assert ordered == [(early, "issuer:mmm"), (early, "issuer:zzz"), (late, "issuer:aaa")]


def test_latest_run_tie_break_is_deterministic_when_executed_at_matches(connection) -> None:
    """Two runs sharing the same executed_at (e.g. a retried materialization)
    must not make "the latest run" ambiguous -- repeated calls must agree,
    and the more recently inserted run must win.

    Real "large_model_value_v0" strategy_key for the same Literal reason as
    above; `tied_at=now()` (rather than a fixed past date) guarantees these
    two rows outrank any earlier local run under the same key, so this test
    reads back exactly one of *these* two rows, not some other survivor.
    """
    strategy_key = "large_model_value_v0"
    tied_at = datetime.now(UTC)
    first = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    second = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, first, strategy_key, executed_at=tied_at)
    _insert_run(connection, second, strategy_key, executed_at=tied_at)
    _insert_decision(
        connection,
        "strategy-decision:" + uuid.uuid4().hex + "0" * 32,
        first,
        issuer_id="issuer:first",
        cutoff_at=tied_at,
    )
    _insert_decision(
        connection,
        "strategy-decision:" + uuid.uuid4().hex + "0" * 32,
        second,
        issuer_id="issuer:second",
        cutoff_at=tied_at,
    )

    repository = PostgresStrategyRunRepository(database_url=_resolve_database_url())
    results = [repository.get_latest(strategy_id=strategy_key, context=_context()) for _ in range(5)]
    assert all(isinstance(r, StrategyRunReport) for r in results)
    winning_issuers = {r.decisions[0].issuer_id for r in results}  # type: ignore[union-attr]

    assert winning_issuers == {"issuer:second"}  # inserted later -> wins the created_at tie-break


def test_ignores_runs_for_a_different_strategy_key(connection) -> None:
    requested_key = _unique_strategy_key()
    other_key = _unique_strategy_key()
    other_run = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, other_run, other_key, executed_at=datetime(2026, 1, 1, tzinfo=UTC))

    repository = PostgresStrategyRunRepository(database_url=_resolve_database_url())
    result = repository.get_latest(strategy_id=requested_key, context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "no_runs_recorded"


def test_fails_closed_on_database_unavailable() -> None:
    repository = PostgresStrategyRunRepository(database_url="postgresql://nope:nope@127.0.0.1:1/does_not_exist")
    result = repository.get_latest(strategy_id="whatever", context=_context())

    assert isinstance(result, StrategyRunUnavailable)
    assert result.reason == "database_unavailable"
