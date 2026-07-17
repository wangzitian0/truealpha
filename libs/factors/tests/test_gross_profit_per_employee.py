from datetime import UTC, datetime
from decimal import Decimal

from factors.base.gross_profit_per_employee import gross_profit_per_employee
from factors.types import Fact

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_RISK_FREE_RATE = Decimal("0.05")


def _fact(metric: str, value, *, entity_id="issuer.acme", confidence="0.9", fiscal_period="2025-12-31") -> Fact:
    return Fact(
        entity_id=entity_id,
        metric=metric,
        value=value,
        confidence=confidence,
        as_of=_AS_OF,
        fiscal_period=fiscal_period,
    )


def test_computes_capital_adjusted_labor_efficiency():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employee_headcount", "100"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    # real_profit = 1_000_000 - 4_000_000 * 0.05 = 800_000; / 100 headcount = 8_000
    assert result.value == Decimal("8000")
    assert result.confidence == Decimal("0.9")
    assert result.flags == []


def test_missing_gross_profit_surfaces_flag_not_silent_drop():
    facts = [
        _fact("total_assets", "4000000"),
        _fact("employee_headcount", "100"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert result.confidence == Decimal("0")
    assert "missing_gross_profit" in result.flags


def test_financial_issuer_without_gross_profit_is_unavailable_under_v0():
    # #59 v0 deliberately does not substitute pre_provision_profit for financials.
    facts = [
        _fact("total_assets", "9000000"),
        _fact("employee_headcount", "50"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "missing_gross_profit" in result.flags


def test_non_positive_headcount_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employee_headcount", "0"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "non_positive_employee_headcount" in result.flags


def test_fiscal_period_mismatch_is_unavailable():
    facts = [
        _fact("gross_profit", "1000000", fiscal_period="2025-12-31"),
        _fact("total_assets", "4000000", fiscal_period="2025-09-30"),
        _fact("employee_headcount", "100", fiscal_period="2025-12-31"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.value is None
    assert "fiscal_period_mismatch" in result.flags


def test_confidence_is_the_minimum_across_inputs():
    facts = [
        _fact("gross_profit", "1000000", confidence="0.9"),
        _fact("total_assets", "4000000", confidence="0.6"),
        _fact("employee_headcount", "100", confidence="0.95"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.confidence == Decimal("0.6")


def test_data_availability_never_overclaims_verified():
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employee_headcount", "100"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert result.data_availability == "unverified"
