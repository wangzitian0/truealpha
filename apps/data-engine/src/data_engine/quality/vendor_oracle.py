"""Re-derive mart financial facts from SEC company-facts to detect reporting drift."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET

_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
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


def _http_json(url: str, user_agent: str, key: FetchKey) -> dict[str, Any]:
    from data_engine.sources import gateway

    def fetch(identity: FetchKey) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        _status, body = gateway.urlopen(
            "sec",
            "vendor-oracle",
            request,
            identity=identity,
            caller="quality.vendor_oracle",
            timeout=90,
            raise_for_status=True,
        )
        return body

    return json.loads(gateway.fetch_once(key, fetch).decode())


def ticker_cik_index(user_agent: str) -> dict[str, int]:
    from data_engine.sources import sec

    payload = _http_json(_TICKERS_URL, user_agent, sec.company_tickers_key())
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()}


def company_facts(cik: int, user_agent: str) -> dict[str, Any]:
    from data_engine.sources import sec

    return _http_json(_COMPANY_FACTS_URL.format(cik=cik), user_agent, sec.company_facts_key(cik))


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
