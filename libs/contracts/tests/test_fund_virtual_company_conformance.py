"""#727: the factor side of the fund-consolidation conformance case.

`libs/contracts/conformance/fund_virtual_company.json` pins one fund's filed lines, the
consolidation the module-5 factor computes from them, and the strings the App must render.
This test asserts the Python half; `apps/app-web/tests/fund-virtual-company-conformance.test.ts`
asserts the TypeScript half against the same file. The App no longer computes the
aggregate, so what this pair guards is that the number the factor produces is the number
the reader shows — the two can only agree by both matching the fixture.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from truealpha_contracts.etf_virtual_company import ETF_CONSOLIDATION_V0, EtfConsolidationDefinition

FIXTURE = Path(__file__).resolve().parents[1] / "conformance" / "fund_virtual_company.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _consolidate(data: dict):
    # Imported here so `libs/contracts` keeps no import-time dependency on `libs/factors`
    # (contracts is the lower layer; only this test crosses).
    from factors.base.etf_virtual_company import HoldingLine, consolidate_fund

    lines = [
        HoldingLine(
            holding_name=line["holding_name"],
            weight=Decimal(line["weight"]),
            listing_id=line["listing_id"],
            valuation_gap=None if line["valuation_gap"] is None else Decimal(line["valuation_gap"]),
            availability=line["availability"],
            confidence=None if line["confidence"] is None else Decimal(line["confidence"]),
        )
        for line in data["lines"]
    ]
    return consolidate_fund(
        lines,
        fund_id=data["fund_id"],
        as_of=datetime.fromisoformat(data["cutoff"]),
        definition=ETF_CONSOLIDATION_V0,
    )


def test_the_pinned_definition_is_the_one_shipped() -> None:
    """The fixture's numbers are only meaningful under the definition that produced them."""
    data = _fixture()["definition"]
    assert data["content_sha256"] == ETF_CONSOLIDATION_V0.content_sha256
    assert data["factor_version"] == ETF_CONSOLIDATION_V0.factor_version
    rebuilt = EtfConsolidationDefinition(
        factor_version=data["factor_version"],
        minimum_resolved_weight=Decimal(data["minimum_resolved_weight"]),
        minimum_valued_weight=Decimal(data["minimum_valued_weight"]),
    )
    assert rebuilt.content_sha256 == ETF_CONSOLIDATION_V0.content_sha256


def test_the_factor_reproduces_the_pinned_consolidation() -> None:
    data = _fixture()
    expected = data["expected"]
    result = _consolidate(data)

    assert result.weighted_valuation_gap == Decimal(expected["weighted_valuation_gap"])
    assert result.total_weight == Decimal(expected["total_weight_pct"])
    assert result.resolved_weight == Decimal(expected["resolved_weight_pct"])
    assert result.valued_weight == Decimal(expected["valued_weight_pct"])
    assert result.unresolved_weight == Decimal(expected["unresolved_weight_pct"])
    assert result.unvalued_weight == Decimal(expected["unvalued_weight_pct"])
    assert (result.lines, result.valued_lines) == (expected["lines"], expected["valued_lines"])
    assert result.result.confidence == Decimal(expected["confidence"])
    assert sorted(result.result.flags) == sorted(expected["flags"])
    assert result.result.data_availability == expected["data_availability"]


def test_the_pinned_gap_is_the_hand_calculation() -> None:
    """Recomputed from the fixture's own lines by a second, independent expression: the
    fixture cannot drift into agreeing with a broken factor, because this arithmetic does
    not go through the factor at all (#36: "weighted metrics match hand calculations")."""
    data = _fixture()
    numerator = sum(
        Decimal(line["weight"]) * Decimal(line["valuation_gap"])
        for line in data["lines"]
        if line["availability"] == "available" and line["valuation_gap"] is not None
    )
    denominator = sum(
        Decimal(line["weight"])
        for line in data["lines"]
        if line["availability"] == "available" and line["valuation_gap"] is not None
    )
    assert numerator / denominator == Decimal(data["expected"]["weighted_valuation_gap"])


def test_the_masses_account_for_every_filed_line() -> None:
    expected = _fixture()["expected"]
    total = Decimal(expected["total_weight_pct"])
    parts = (
        Decimal(expected["valued_weight_pct"])
        + Decimal(expected["unvalued_weight_pct"])
        + Decimal(expected["unresolved_weight_pct"])
    )
    assert parts == total, "valued + unvalued + unresolved is the whole filed mass"
