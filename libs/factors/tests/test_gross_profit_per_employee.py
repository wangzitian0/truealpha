from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest
from factors.base.gross_profit_per_employee import (
    GPPE_EXPRESSION_DEFINITION,
    gross_profit_per_employee,
)
from factors.expressions.compiler import compile_expression
from factors.types import Fact, UnitFamily

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_RISK_FREE_RATE = Decimal("0.05")
_UNIT_FAMILY = {
    "gross_profit": UnitFamily.CURRENCY,
    "total_assets": UnitFamily.CURRENCY,
    "employees_total": UnitFamily.COUNT,
    "stockholders_equity": UnitFamily.CURRENCY,
    "goodwill": UnitFamily.CURRENCY,
    "intangible_assets_net_excluding_goodwill": UnitFamily.CURRENCY,
    "preferred_stock_value": UnitFamily.CURRENCY,
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
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
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


def test_uniform_charge_v0_applies_to_banks_and_is_signal_negative():
    # gppe_uniform_charge_v0 is the published value for every class, banks included. A large
    # balance sheet drives real profit negative: a valid low signal, not an exclusion. The
    # strategy ranks banks on gppe_banking_tce_v1 instead (test_strategy_evaluator.py).
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


def test_polars_expression_reproduces_the_decimal_result():
    """Matrix-compatible cross-check: the compiled Polars execution of
    GPPE_EXPRESSION_DEFINITION must reproduce the same value as the native Decimal
    computation above (#26 acceptance style — independent oracle and vectorised
    engine agree).

    Not gated on an optional import. The previous engine's version of this test
    skipped whenever its runtime was absent — 11 of the last 12 ci-python runs —
    so the reproducibility proof was effectively unarmed (#956, #969). `polars`
    is a first-class dependency, so this executes in ci-python every run.
    """
    panel = pl.DataFrame(
        {
            "symbol": ["issuer.acme"],
            "date": [date(2026, 6, 30)],
            "gross_profit": [1_000_000.0],
            "total_assets": [4_000_000.0],
            "risk_free_rate": [0.05],
            "employees_total": [100.0],
        }
    )

    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "4000000"),
        _fact("employees_total", "100"),
    ]
    native = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    assert native.value is not None, native.flags

    compiled = panel.with_columns(factor_value=compile_expression(GPPE_EXPRESSION_DEFINITION))
    vectorised = compiled["factor_value"].to_list()[0]

    # real_profit = 1_000_000 - 4_000_000 * 0.05 = 800_000; / 100 headcount = 8_000
    assert native.value == Decimal("8000")
    assert vectorised == pytest.approx(float(native.value), rel=1e-12)


def test_non_financial_above_fifteen_times_gross_profit_takes_the_uniform_charge() -> None:
    # Assets / gross profit = 50.9 > 15. The retired leverage branch replaced the assets base with
    # 8% of assets for ANY issuer above this ratio, so a non-financial issuer took it too. The
    # uniform charge applies to every class: the base is total_assets, with no ratio test.
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.acme"),
        _fact("total_assets", "4424900000000", entity_id="issuer.acme"),
        _fact("employees_total", "318512", entity_id="issuer.acme"),
    ]
    result = gross_profit_per_employee(facts, entity_id="issuer.acme", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE)
    expected = (Decimal("86807000000") - Decimal("4424900000000") * _RISK_FREE_RATE) / Decimal("318512")
    assert result.value == expected
    assert result.value < 0
    assert result.flags == []


def _banking_facts(
    *,
    equity: str | None = "340000000000",
    preferred: str | None = "10000000000",
    goodwill: str | None = "5000000000",
    intangibles: str | None = "1000000000",
    total_assets: str | None = "4424900000000",
) -> list[Fact]:
    # Synthetic bank inputs. Tangible common equity = equity - preferred - (goodwill + intangibles) = 324B.
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    if total_assets is not None:
        facts.append(_fact("total_assets", total_assets, entity_id="issuer.bank"))
    if equity is not None:
        facts.append(_fact("stockholders_equity", equity, entity_id="issuer.bank"))
    if preferred is not None:
        facts.append(_fact("preferred_stock_value", preferred, entity_id="issuer.bank"))
    if goodwill is not None:
        facts.append(_fact("goodwill", goodwill, entity_id="issuer.bank"))
    if intangibles is not None:
        facts.append(_fact("intangible_assets_net_excluding_goodwill", intangibles, entity_id="issuer.bank"))
    return facts


def test_banking_tce_charges_measured_tangible_common_equity() -> None:
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    tangible_common_equity = (
        Decimal("340000000000") - Decimal("10000000000") - Decimal("5000000000") - Decimal("1000000000")
    )
    expected = (Decimal("86807000000") - tangible_common_equity * _RISK_FREE_RATE) / Decimal("318512")
    assert result.value == expected
    assert result.value > Decimal("200000")  # positive on this synthetic base
    assert result.unit_family == UnitFamily.PER_EMPLOYEE
    assert result.flags == []


def test_banking_tce_deducts_preferred_stock_for_jpm_fy2025() -> None:
    # JPM FY2025 10-K (accession 0001628280-26-008131, repo sample apps/data-engine/samples/filings):
    # total stockholders' equity 362,438M; preferred stock 20,045M (362,438M - 342,393M common equity);
    # goodwill 52,731M; intangibles 2,600M (finite-lived 1,300M + indefinite-lived 1,300M);
    # employees 318,512; pre-provision profit 86,807M. Risk-free rate 0.05.
    # Hand computation:
    #   TCE          = 362,438M - 20,045M - 52,731M - 2,600M = 287,062M
    #   capital      = 287,062M x 0.05                       = 14,353.1M
    #   real profit  = 86,807M - 14,353.1M                   = 72,453.9M
    #   value        = 72,453.9M / 318,512                   = about +227,476 per employee
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    facts = _banking_facts(
        equity="362438000000",
        preferred="20045000000",
        goodwill="52731000000",
        intangibles="2600000000",
        total_assets=None,
    )
    result = gross_profit_per_employee_banking_tce(
        facts, entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    assert result.value == Decimal("72453900000") / Decimal("318512")
    assert result.flags == []


def test_banking_tce_missing_gives_unavailable_with_reason() -> None:
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(goodwill=None), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    assert result.value is None
    assert result.confidence == Decimal("0")
    assert "missing_tangible_common_equity" in result.flags


def test_banking_missing_preferred_stock_gives_unavailable_with_its_own_reason() -> None:
    # No zero stand-in for preferred stock: the measurement is refused, not computed on common equity.
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(preferred=None), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    assert result.value is None
    assert result.confidence == Decimal("0")
    assert "missing_preferred_stock_value" in result.flags
    assert "missing_tangible_common_equity" not in result.flags


def test_banking_reported_zero_preferred_stock_is_a_value_not_a_gap() -> None:
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(preferred="0"), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    tangible_common_equity = Decimal("340000000000") - Decimal("5000000000") - Decimal("1000000000")
    assert result.value == (Decimal("86807000000") - tangible_common_equity * _RISK_FREE_RATE) / Decimal("318512")
    assert result.flags == []


def test_banking_tce_never_falls_back_to_total_assets() -> None:
    # Total assets are present and large. Without a measured equity, the value stays unavailable:
    # no ratio test, no 8% stand-in, no assets base.
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(equity=None), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    assert result.value is None
    assert "missing_tangible_common_equity" in result.flags


def test_banking_tce_does_not_require_total_assets() -> None:
    from factors.base.gross_profit_per_employee import gross_profit_per_employee_banking_tce

    result = gross_profit_per_employee_banking_tce(
        _banking_facts(total_assets=None), entity_id="issuer.bank", as_of=_AS_OF, risk_free_rate=_RISK_FREE_RATE
    )
    assert result.value is not None
    assert result.flags == []
