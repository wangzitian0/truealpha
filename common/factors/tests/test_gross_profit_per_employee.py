import sys
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

import polars as pl
import pytest
from factors.base.gross_profit_per_employee import (
    GPPE_EXPRESSION_DEFINITION,
)
from factors.base.gross_profit_per_employee import (
    gross_profit_per_employee as _factor,
)
from factors.forest import FOREST, GPPE_V0_TREE, IssuerClass
from factors.types import Fact, FactorResult, UnitFamily

# The Polars AST compiler is a test-time oracle under tests/support (init.md rule 25, #758).
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from expression_oracle.compiler import compile_expression  # noqa: E402

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


def gross_profit_per_employee(
    facts: list[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    risk_free_rate: Decimal,
    issuer_class: IssuerClass = IssuerClass.NON_FINANCIAL,
) -> FactorResult:
    """The factor on the uniform tree `gppe` (`gppe_uniform_charge_v0`), the tree of the frozen #21 replay."""
    return _factor(
        facts,
        entity_id=entity_id,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
        forest=FOREST,
        tree=GPPE_V0_TREE,
        issuer_class=issuer_class,
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
    result = gross_profit_per_employee(
        facts,
        entity_id="issuer.bank",
        as_of=_AS_OF,
        risk_free_rate=_RISK_FREE_RATE,
        issuer_class=IssuerClass.FINANCIAL,
    )
    assert result.value is None
    assert "missing_total_assets" in result.flags


def test_uniform_charge_v0_applies_to_banks_and_is_signal_negative():
    # gppe_uniform_charge_v0 is the published value for every class, banks included. A large
    # balance sheet drives real profit negative: a valid low signal, not an exclusion. The
    # definition pins the banking tree for banks instead (test_bound_tree.py, test_strategy_evaluator.py).
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank"),
        _fact("employees_total", "318512", entity_id="issuer.bank"),
    ]
    result = gross_profit_per_employee(
        facts,
        entity_id="issuer.bank",
        as_of=_AS_OF,
        risk_free_rate=_RISK_FREE_RATE,
        issuer_class=IssuerClass.FINANCIAL,
    )
    # The tree divides in its own 34-digit context; the expected value is written out in the same context.
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        real_profit = Decimal("86807000000") - Decimal("4424900000000") * _RISK_FREE_RATE
        expected = real_profit / Decimal("318512")
    assert result.value == expected
    assert result.value < 0
    assert result.unit_family == UnitFamily.PER_EMPLOYEE
    assert result.flags == []


def test_confidence_includes_total_assets_for_every_issuer():
    facts = [
        _fact("gross_profit", "86807000000", entity_id="issuer.bank", confidence="0.9"),
        _fact("total_assets", "4424900000000", entity_id="issuer.bank", confidence="0.1"),
        _fact("employees_total", "318512", entity_id="issuer.bank", confidence="0.8"),
    ]
    result = gross_profit_per_employee(
        facts,
        entity_id="issuer.bank",
        as_of=_AS_OF,
        risk_free_rate=_RISK_FREE_RATE,
        issuer_class=IssuerClass.FINANCIAL,
    )
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
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        expected = (Decimal("86807000000") - Decimal("4424900000000") * _RISK_FREE_RATE) / Decimal("318512")
    assert result.value == expected
    assert result.value < 0
    assert result.flags == []
