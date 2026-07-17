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
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
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
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
    )
    assert result.value is None
    assert result.confidence == Decimal("0")
    assert "missing_gross_profit" in result.flags


def test_financial_issuer_without_gross_profit_is_unavailable():
    # gross_profit is the parser's industry-branch definition for financial
    # issuers too (metrics.py: financial_issuer_split=True) — its absence is
    # a real data gap, not a deferred feature.
    facts = [
        _fact("employees_total", "50"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="financial"
    )
    assert result.value is None
    assert "missing_gross_profit" in result.flags


def test_financial_issuer_computes_without_capital_charge():
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="financial"
    )
    # 86_807_000_000 / 318_512 = 272_539.182...; no total_assets/risk_free_rate involved.
    assert result.value == (Decimal("86807000000") / Decimal("318512"))
    assert result.unit_family == UnitFamily.PER_EMPLOYEE
    assert result.confidence == Decimal("0.9")
    assert result.flags == []


def test_financial_issuer_does_not_require_total_assets():
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    with_total_assets = gross_profit_per_employee(
        facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="financial"
    )
    without_total_assets = gross_profit_per_employee(
        [f for f in facts if f.metric != "total_assets"],
        entity_id="issuer.bank",
        as_of=_AS_OF,
        risk_free_rate=_RISK_FREE_RATE,
        issuer_branch="financial",
    )
    # An extraneous total_assets fact must not change the financial branch's
    # output — it is not consumed, so its presence or absence is irrelevant.
    assert with_total_assets.value == without_total_assets.value
    assert without_total_assets.flags == []


def test_financial_issuer_confidence_ignores_total_assets():
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank", confidence="0.9"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank", confidence="0.1"),
        _fact("employees_total", "318512", entity_id="issuer.bank", confidence="0.8"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="financial"
    )
    # min(gross_profit, employees_total) only — the low-confidence total_assets fact is unused.
    assert result.confidence == Decimal("0.8")


def test_non_positive_headcount_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "0"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
    )
    assert result.value is None
    assert "non_positive_employees_total" in result.flags


def test_fiscal_period_mismatch_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000", fiscal_period="2025-12-31"),
        _fact("total_assets", "4000000", fiscal_period="2025-09-30"),
        _fact("employees_total", "100", fiscal_period="2025-12-31"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
    )
    assert result.value is None
    assert "fiscal_period_mismatch" in result.flags


def test_confidence_is_the_minimum_across_inputs():
    facts = [
        _fact("gross_profit", "1000000", confidence="0.9"),
        _fact("total_assets", "4000000", confidence="0.6"),
        _fact("employees_total", "100", confidence="0.95"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
    )
    assert result.confidence == Decimal("0.6")


def test_data_availability_never_overclaims_verified():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "100"),
    ]
    result = gross_profit_per_employee(
        facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE, issuer_branch="non_financial"
    )
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
