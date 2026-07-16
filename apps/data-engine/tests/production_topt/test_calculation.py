from datetime import UTC, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.production_topt import GppeCalculationInput, calculate_gppe

CUTOFF = datetime(2026, 3, 31, tzinfo=UTC)


@pytest.mark.parametrize(
    ("gross_profit", "headcount", "expected_gppe", "expected_tier", "expected_band"),
    (
        ("999999", 1, "999999", "traditional", ("3", "4")),
        ("1000000", 1, "1000000", "tech", ("8", "10")),
        ("2999999", 1, "2999999", "tech", ("8", "10")),
        ("3000000", 1, "3000000", "large_model_native", ("20", "30")),
        ("10000000", 4, "2500000", "tech", ("8", "10")),
    ),
)
def test_gppe_uses_exact_decimal_tier_boundaries(
    gross_profit: str,
    headcount: int,
    expected_gppe: str,
    expected_tier: str,
    expected_band: tuple[str, str],
) -> None:
    result = calculate_gppe(
        GppeCalculationInput(
            run_id="capture-run:" + "1" * 64,
            issuer_id="issuer:lei:EXAMPLE",
            cutoff=CUTOFF,
            gross_profit=Decimal(gross_profit),
            employee_headcount=headcount,
            is_financial=False,
            confidence=Decimal("0.9"),
        )
    )

    assert result.availability == "available"
    assert result.gppe == Decimal(expected_gppe)
    assert result.tier == expected_tier
    assert (result.target_ps_lower, result.target_ps_upper) == tuple(map(Decimal, expected_band))
    assert result.reason_codes == ()


@pytest.mark.parametrize(
    ("gross_profit", "headcount", "is_financial", "reason"),
    (
        (None, 10, False, "missing_gross_profit"),
        (Decimal("10"), None, False, "missing_employee_headcount"),
        (Decimal("10"), 0, False, "nonpositive_employee_headcount"),
        (Decimal("10"), 10, True, "financial_gppe_mapping_unapproved"),
    ),
)
def test_gppe_keeps_unavailable_issuers_explicit(gross_profit, headcount, is_financial: bool, reason: str) -> None:
    result = calculate_gppe(
        GppeCalculationInput(
            run_id="capture-run:" + "1" * 64,
            issuer_id="issuer:lei:EXAMPLE",
            cutoff=CUTOFF,
            gross_profit=gross_profit,
            employee_headcount=headcount,
            is_financial=is_financial,
            confidence=Decimal("0.9"),
        )
    )

    assert result.availability == "unavailable"
    assert result.gppe is result.tier is None
    assert result.confidence == 0
    assert result.reason_codes == (reason,)
