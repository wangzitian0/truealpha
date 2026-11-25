"""Re-derive mart financial facts from SEC company-facts to detect reporting drift."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from data_engine.datahub.capture_core.concept_mapping import DEFAULT_RULESET
from data_engine.sources import sec

_ANNUAL_MINIMUM_DAYS = 350


def _ruleset_us_gaap_concepts(*fields: str, extra: Sequence[str] = ()) -> tuple[str, ...]:
    concepts = [
        item.concept
        for field in fields
        if (mapping := DEFAULT_RULESET.mapping_for(field)) is not None
        for item in mapping.concepts
        if item.taxonomy == "us-gaap"
    ]
    return tuple(dict.fromkeys([*concepts, *extra]))


_EXTRA_REVENUE = (
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "PremiumsEarnedNet",
    "FinancialServicesRevenue",
    "HealthCareOrganizationRevenue",
    "OperatingLeasesIncomeStatementLeaseRevenue",
)
_REVENUE_CONCEPTS = _ruleset_us_gaap_concepts(
    "revenue", "bank_revenue", "interest_income_expense_net", "noninterest_income", extra=_EXTRA_REVENUE
)
_COGS_CONCEPTS = _ruleset_us_gaap_concepts("cost_of_revenue", "insurance_claims")
_ASSETS_CONCEPTS = _ruleset_us_gaap_concepts("total_assets")


@dataclass(frozen=True)
class VendorFact:
    value: Decimal
    period_end: date
    concept: str
    filed: date


def ticker_cik_index(user_agent: str) -> dict[str, int]:
    with sec.client(user_agent=user_agent, timeout=90.0) as http:
        return sec.ticker_cik_index(http)


def company_facts(cik: int, user_agent: str) -> dict[str, Any]:
    with sec.client(user_agent=user_agent, timeout=90.0) as http:
        return sec.fetch_company_facts(cik, http)


def _annual_entries(
    facts: dict[str, Any], concept: str, unit: str, cutoff: date
) -> Iterable[tuple[date, Decimal, date]]:
    entries = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit) or []
    for entry in entries:
        filed_raw, end_raw, start_raw, value_raw = (
            entry.get("filed"),
            entry.get("end"),
            entry.get("start"),
            entry.get("val"),
        )
        if not filed_raw or not end_raw or value_raw is None:
            continue
        try:
            filed, end = date.fromisoformat(filed_raw), date.fromisoformat(end_raw)
            if start_raw is not None and (end - date.fromisoformat(start_raw)).days < _ANNUAL_MINIMUM_DAYS:
                continue
            value = Decimal(str(value_raw))
        except (InvalidOperation, ValueError):
            continue
        if filed <= cutoff:
            yield end, value, filed


def latest_across_variants(
    facts: dict[str, Any], concepts: Sequence[str], unit: str, cutoff: date
) -> VendorFact | None:
    best: VendorFact | None = None
    for concept in concepts:
        for period_end, value, filed in _annual_entries(facts, concept, unit, cutoff):
            if best is None or period_end > best.period_end:
                best = VendorFact(value=value, period_end=period_end, concept=concept, filed=filed)
    return best


def revenue(facts: dict[str, Any], cutoff: date) -> VendorFact | None:
    return latest_across_variants(facts, _REVENUE_CONCEPTS, "USD", cutoff)


def total_assets(facts: dict[str, Any], cutoff: date) -> VendorFact | None:
    return latest_across_variants(facts, _ASSETS_CONCEPTS, "USD", cutoff)


def gross_profit(facts: dict[str, Any], cutoff: date) -> VendorFact | None:
    direct = latest_across_variants(facts, ("GrossProfit",), "USD", cutoff)
    revenue_by_period = _all_periods(facts, _REVENUE_CONCEPTS, cutoff)
    cost_by_period = _all_periods(facts, _COGS_CONCEPTS, cutoff)
    shared = set(revenue_by_period) & set(cost_by_period)
    derived: VendorFact | None = None
    if shared:
        end = max(shared)
        (rev_val, rev_filed), (cost_val, cost_filed) = revenue_by_period[end], cost_by_period[end]
        derived = VendorFact(
            value=rev_val - cost_val,
            period_end=end,
            concept="Revenues-CostOfRevenue",
            filed=max(rev_filed, cost_filed),
        )
    candidates = [item for item in (direct, derived) if item is not None]
    return max(candidates, key=lambda item: item.period_end) if candidates else None


def _all_periods(facts: dict[str, Any], concepts: Sequence[str], cutoff: date) -> dict[date, tuple[Decimal, date]]:
    merged: dict[date, tuple[Decimal, date]] = {}
    for concept in concepts:
        for period_end, value, filed in _annual_entries(facts, concept, "USD", cutoff):
            merged.setdefault(period_end, (value, filed))
    return merged
