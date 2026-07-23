"""#469: twin-parity conformance, Python half (against a real local Postgres;
skips gracefully without one, fails hard when TRUEALPHA_REQUIRE_RUNTIME is set).

Seeds the shared cases from ``conformance/strategy_run_parity.json`` and asserts
the shipping Python consumer's serialized output equals the frozen canon that
the TS twin (``apps/app-web/tests/strategy-run-parity-conformance.test.ts``)
must also produce — two languages, one schema, one expected byte shape. The
divergence-trigger cases (a run recorded under a non-default key, ``rank = 0``,
``target_weight = 1.5``) must come back ``schema_mismatch`` on BOTH sides.

Everything runs inside ONE transaction: the repository under test opens its own
connection per call, so ``psycopg.connect`` is redirected onto the test's
connection (the same seam the TS half injects through ``db.ts __setTestClient``,
and the same technique as test_materialization's ``_BorrowedConnection``).
Nothing is committed — the fixture's fixed far-future ``executed_at`` can never
poison other tests' "latest run" isolation on a shared database.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository

_FIXTURE_PATH = Path(__file__).parents[1] / "conformance" / "strategy_run_parity.json"
_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"

_RUN_SQL = """insert into mart.strategy_runs
 (strategy_run_id, content_sha256, strategy_key, strategy_version, definition_content_sha256,
  corpus_sha256, claim_ceiling, executed_at)
 values (%(strategy_run_id)s, %(content_sha256)s, %(strategy_key)s, %(strategy_version)s,
  %(definition_content_sha256)s, %(corpus_sha256)s, %(claim_ceiling)s, %(executed_at)s)"""

_DECISION_SQL = """insert into mart.strategy_decisions
 (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
  capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
  valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight)
 values (%(strategy_decision_id)s, %(content_sha256)s, %(strategy_run_id)s, %(issuer_id)s,
  %(cutoff_at)s, %(capital_adjusted_labor_efficiency)s, %(tier)s, %(current_price_to_sales)s,
  %(target_price_to_sales)s, %(valuation_gap)s, %(eligible)s, %(outcome)s, %(exclusion_reason)s,
  %(rank)s, %(target_weight)s)"""


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


class _BorrowedConnection:
    """Redirects the repository's per-call ``psycopg.connect`` onto the test's
    own transaction; close/commit are withheld so the rollback owns the data."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **kwargs):
        return self._real.cursor(**kwargs)


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch):
    fixture = json.loads(_FIXTURE_PATH.read_text())
    try:
        connection = psycopg.connect(_database_url(), connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; ci-python runs this against a real database")
    try:
        for case in fixture.values():
            connection.execute(_RUN_SQL, case["run"])
            for decision in case["decisions"]:
                connection.execute(_DECISION_SQL, {**decision, "strategy_run_id": case["run"]["strategy_run_id"]})
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
        yield fixture
    finally:
        connection.rollback()
        connection.close()


def _context() -> AccessContext:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    return AccessContext(
        context_id="ctx:parity",
        principal_id="principal:parity",
        tenant_id="tenant:parity",
        session_id="session:parity",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )


def test_report_serialization_matches_the_frozen_canon(seeded) -> None:
    case = seeded["report"]
    repository = PostgresStrategyRunRepository(database_url=_database_url())
    result = repository.get_latest(strategy_id=case["request_strategy_id"], context=_context())
    assert isinstance(result, StrategyRunReport), f"expected a report, got {result!r}"
    assert result.model_dump(mode="json") == case["expected_report"]


def test_divergence_triggers_are_schema_mismatch(seeded) -> None:
    repository = PostgresStrategyRunRepository(database_url=_database_url())
    for name in ("wrong_strategy_id", "rank_zero", "weight_out_of_bounds"):
        case = seeded[name]
        result = repository.get_latest(strategy_id=case["request_strategy_id"], context=_context())
        assert isinstance(result, StrategyRunUnavailable), f"{name}: expected unavailable, got {result!r}"
        assert result.reason == case["expected_reason"], f"{name}: {result.reason}"
