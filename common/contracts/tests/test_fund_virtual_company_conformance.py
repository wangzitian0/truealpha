"""#727: fund-consolidation conformance case."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from truealpha_contracts.etf_virtual_company import ETF_CONSOLIDATION_V0, EtfConsolidationDefinition

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fund_virtual_company.json"


def test_fund_virtual_company_conformance() -> None:
    from factors.base.etf_virtual_company import HoldingLine, consolidate_fund

    data = json.loads(FIXTURE.read_text())
    dfn, exp = data["definition"], data["expected"]
    assert (dfn["content_sha256"], dfn["factor_version"]) == (
        ETF_CONSOLIDATION_V0.content_sha256,
        ETF_CONSOLIDATION_V0.factor_version,
    )
    rebuilt = EtfConsolidationDefinition(
        factor_version=dfn["factor_version"],
        minimum_resolved_weight=Decimal(dfn["minimum_resolved_weight"]),
        minimum_valued_weight=Decimal(dfn["minimum_valued_weight"]),
    )
    assert rebuilt.content_sha256 == ETF_CONSOLIDATION_V0.content_sha256

    def _d(x: str | None) -> Decimal | None:
        return None if x is None else Decimal(x)

    lines = [
        HoldingLine(
            ln["holding_name"],
            Decimal(ln["weight"]),
            ln["listing_id"],
            _d(ln["valuation_gap"]),
            ln["availability"],
            _d(ln["confidence"]),
        )
        for ln in data["lines"]
    ]
    res = consolidate_fund(
        lines, fund_id=data["fund_id"], as_of=datetime.fromisoformat(data["cutoff"]), definition=ETF_CONSOLIDATION_V0
    )
    assert res.weighted_valuation_gap == Decimal(exp["weighted_valuation_gap"]) and res.total_weight == Decimal(
        exp["total_weight_pct"]
    )
    got = (res.resolved_weight, res.valued_weight, res.unresolved_weight, res.unvalued_weight)
    assert got == tuple(
        Decimal(exp[k])
        for k in ("resolved_weight_pct", "valued_weight_pct", "unresolved_weight_pct", "unvalued_weight_pct")
    )
    assert (
        res.lines,
        res.valued_lines,
        res.result.confidence,
        sorted(res.result.flags),
        res.result.data_availability,
    ) == (
        exp["lines"],
        exp["valued_lines"],
        Decimal(exp["confidence"]),
        sorted(exp["flags"]),
        exp["data_availability"],
    )
    av = [ln for ln in data["lines"] if ln["availability"] == "available" and ln["valuation_gap"] is not None]
    assert sum(Decimal(ln["weight"]) * Decimal(ln["valuation_gap"]) for ln in av) / sum(
        Decimal(ln["weight"]) for ln in av
    ) == Decimal(exp["weighted_valuation_gap"])
    assert sum(
        Decimal(exp[k]) for k in ("valued_weight_pct", "unvalued_weight_pct", "unresolved_weight_pct")
    ) == Decimal(exp["total_weight_pct"])
