"""#1235: the P/GP columns of definition 1.0.0 reach mart, and a P/S column never holds a P/GP value.

Migration ``20261013T0201_factors_price_to_gross_profit.sql`` adds named P/GP columns to
``mart.strategy_decisions`` and ``mart.topt_core_results``. These tests run against a
database built from the chain (``make db-reset``) and fail on a chain without it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.core_strategy_replay import Decision
from data_engine.strategy_replay_repository import write_replay
from truealpha_contracts.strategy import (
    load_live_strategy_definition,
)

_HASH64 = "b" * 64
_CUTOFF = "2026-10-08T00:00:00+00:00"


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


def _columns(connection, table: str) -> dict[str, str]:
    schema, name = table.split(".")
    rows = connection.execute(
        "select column_name, data_type from information_schema.columns where table_schema = %s and table_name = %s",
        (schema, name),
    ).fetchall()
    return dict(rows)


def test_the_pgp_columns_exist_as_numeric(connection) -> None:
    decisions = _columns(connection, "mart.strategy_decisions")
    core = _columns(connection, "mart.topt_core_results")
    assert decisions.get("current_price_to_gross_profit") == "numeric"
    assert decisions.get("target_price_to_gross_profit") == "numeric"
    for column in ("current_pgp", "target_pgp_lower", "target_pgp_upper", "target_pgp_midpoint"):
        assert core.get(column) == "numeric", column


def _decision(**overrides: object) -> Decision:
    values: dict[str, object] = {
        "issuer_id": "issuer:wmt",
        "cutoff_at": _CUTOFF,
        "capital_adjusted_labor_efficiency": Decimal("74439.00"),
        "tier": "traditional",
        "current_price_to_sales": Decimal("1.2420"),
        "target_price_to_sales": None,
        "valuation_gap": Decimal("0.7056"),
        "eligible": True,
        "outcome": "selected",
        "exclusion_reason": None,
        "rank": 1,
        "target_weight": Decimal("0.500000"),
        "current_price_to_gross_profit": Decimal("5.1302"),
        "target_price_to_gross_profit": Decimal("8.7500"),
    }
    values.update(overrides)
    return Decision(**values)  # type: ignore[arg-type]


def test_a_v1_decision_persists_pgp_and_no_target_ps(connection) -> None:
    definition = load_live_strategy_definition()
    run_id, (decision_id,) = write_replay(
        connection, [_decision()], definition, executed_at=datetime(2026, 10, 8, tzinfo=UTC)
    )
    row = connection.execute(
        """
        select r.definition_content_sha256, r.strategy_version, d.current_price_to_sales,
               d.target_price_to_sales, d.current_price_to_gross_profit, d.target_price_to_gross_profit
        from mart.strategy_decisions d join mart.strategy_runs r using (strategy_run_id)
        where d.strategy_decision_id = %s
        """,
        (decision_id,),
    ).fetchone()
    assert row == (
        definition.content_sha256,
        "1.0.0",
        Decimal("1.2420"),
        None,
        Decimal("5.1302"),
        Decimal("8.7500"),
    )
    assert run_id.startswith("strategy-run:")


def test_a_v0_decision_keeps_its_content_identity() -> None:
    """The P/GP keys enter the content hash only when present, so a decision persisted under
    definition 0.1.0 replays to the same id and never raises an identity conflict."""
    v0 = _decision(
        target_price_to_sales=Decimal("1.1500"), current_price_to_gross_profit=None, target_price_to_gross_profit=None
    )
    assert "current_price_to_gross_profit" not in v0.to_json()
    assert "target_price_to_gross_profit" not in v0.to_json()
    assert _decision().to_json()["target_price_to_gross_profit"] == "8.7500"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"target_price_to_sales": Decimal("1.1500")}, "two target bases on one decision"),
        ({"current_price_to_gross_profit": None}, "a P/GP target without its current P/GP"),
    ],
)
def test_the_mart_refuses_an_ambiguous_valuation_basis(connection, overrides: dict, why: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="strategy_decisions_one_valuation_basis_check"):
        write_replay(
            connection,
            [_decision(**overrides)],
            load_live_strategy_definition(),
            executed_at=datetime(2026, 10, 8, tzinfo=UTC),
        )


def test_the_core_invocation_accepts_a_strategy_definition_id(connection) -> None:
    definition = load_live_strategy_definition()
    constraint = connection.execute(
        """
        select pg_get_constraintdef(oid) from pg_constraint
        where conrelid = 'mart.topt_core_invocations'::regclass
          and conname = 'topt_core_invocations_tier_definition_id_check'
        """
    ).fetchone()
    assert constraint is not None and "strategy-definition" in constraint[0]
    assert definition.strategy_definition_id.startswith("strategy-definition:")
