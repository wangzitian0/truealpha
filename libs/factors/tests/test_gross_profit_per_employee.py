from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.gross_profit_per_employee import (
    GPPE_EXPRESSION_DEFINITION,
    gross_profit_per_employee,
)
from factors.types import Fact, UnitFamily

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_RISK_FREE_RATE = Decimal("0.05")
_UNIT_FAMILY = {
    "gross_profit": UnitFamily.CURRENCY,
    "total_assets": UnitFamily.CURRENCY,
    "employees_total": UnitFamily.COUNT,
}


def _fact(metric: str, value, *, entity_id="issuer.acme", confidence="0.9", fiscal_period="2025-12-31") -> Fact:
    return Fact(
        entity_id=entity_id,
        metric=metric,
        value=value,
        unit_family=_UNIT_FAMILY[metric],
        confidence=confidence,
        as_of=_AS_OF,
        fiscal_period=fiscal_period,
    )


def test_computes_capital_adjusted_labor_efficiency():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "100"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    # real_profit = 1_000_000 - 4_000_000 * 0.05 = 800_000; / 100 headcount = 8_000
    assert result.value == Decimal("8000")
    assert result.unit_family == UnitFamily.PER_EMPLOYEE
    assert result.confidence == Decimal("0.9")
    assert result.flags == []


def test_missing_gross_profit_surfaces_flag_not_silent_drop():
    facts = [
        _fact("total_assets", "4000000"),
        _fact("employees_total", "100"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert result.confidence == Decimal("0")
    assert "missing_gross_profit" in result.flags


def test_missing_total_assets_surfaces_flag_for_every_issuer():
    # The capital charge is uniform: total_assets is required for banks too.
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "missing_total_assets" in result.flags


def test_bank_takes_the_same_capital_adjusted_formula():
    # A financial issuer takes the identical uniform path; a large balance
    # sheet drives real profit — and thus labor efficiency — negative, which
    # is a valid low signal, not a special-case exclusion.
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    real_profit = Decimal("86807000000") - Decimal("4424900000000") * _RISK_FREE_RATE
    assert result.value == real_profit / Decimal("318512")
    assert result.value < 0
    assert result.unit_family == UnitFamily.PER_EMPLOYEE
    assert result.flags == []


def test_confidence_includes_total_assets_for_every_issuer():
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank", confidence="0.9"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank", confidence="0.1"),
        _fact("employees_total", "318512", entity_id="issuer.bank", confidence="0.8"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    # total_assets is now consumed uniformly, so its low confidence bounds the result.
    assert result.confidence == Decimal("0.1")


def test_non_positive_headcount_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "0"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "non_positive_employees_total" in result.flags


def test_fiscal_period_mismatch_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000", fiscal_period="2025-12-31"),
        _fact("total_assets", "4000000", fiscal_period="2025-09-30"),
        _fact("employees_total", "100", fiscal_period="2025-12-31"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "fiscal_period_mismatch" in result.flags


def test_confidence_is_the_minimum_across_inputs():
    facts = [
        _fact("gross_profit", "1000000", confidence="0.9"),
        _fact("total_assets", "4000000", confidence="0.6"),
        _fact("employees_total", "100", confidence="0.95"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.confidence == Decimal("0.6")


def test_data_availability_never_overclaims_verified():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "100"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.data_availability == "unverified"


def test_qlib_expression_reproduces_the_decimal_result():
    """Matrix-compatible cross-check: the pinned Qlib runtime must reproduce
    the same value as the native Decimal computation above (#26 acceptance
    style — independent oracle and Qlib adapter agree)."""

    qlib = pytest.importorskip("qlib")
    del qlib
    from datetime import date

    from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY, evaluate_expression
    from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding

    session = date(2026, 6, 30)
    panel = {
        "gross_profit": {"issuer.acme": (1_000_000.0,)},
        "total_assets": {"issuer.acme": (4_000_000.0,)},
        "risk_free_rate": {"issuer.acme": (0.05,)},
        "employees_total": {"issuer.acme": (100.0,)},
    }
    binding = QlibExpressionExecutionBinding(
        version="0.9.7",
        release_commit="a" * 40,
        runtime_artifact_sha256="b" * 64,
        runtime_lock_sha256="c" * 64,
        adapter_id="factors.qlib_engine.test",
        adapter_implementation_sha256="d" * 64,
    )

    _, outputs, _ = evaluate_expression(
        GPPE_EXPRESSION_DEFINITION,
        BUILTIN_OPERATOR_REGISTRY,
        panel=panel,
        instruments=("issuer.acme",),
        sessions=(session,),
        execution_binding=binding,
    )

    # real_profit = 1_000_000 - 4_000_000 * 0.05 = 800_000; / 100 headcount = 8_000
    assert outputs[("issuer.acme", session)] == pytest.approx(8000.0)
