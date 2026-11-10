# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import urllib.request; from collections.abc import Iterable, Sequence; from dataclasses import dataclass; from datetime import date; from decimal import Decimal, InvalidOperation; from typing import Any; from psycopg import Connection; from truealpha_contracts.universes import id_prefix_like, universe_with_role; from data_engine.datahub.fetch_key import FetchKey; from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET; _COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"; _TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"; _ANNUAL_MINIMUM_DAYS = 350
def _ruleset_us_gaap_concepts(*fields: str, extra: Sequence[str] = ()) -> tuple[str, ...]: concepts = [ item.concept for field in fields if (mapping := DEFAULT_RULESET.mapping_for(field)) is not None for item in mapping.concepts if item.taxonomy == "us-gaap" ]; return tuple(dict.fromkeys([*concepts, *extra]))
_EXTRA_REVENUE = ( "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "PremiumsEarnedNet", "FinancialServicesRevenue", "HealthCareOrganizationRevenue", "OperatingLeasesIncomeStatementLeaseRevenue", ); _REVENUE_CONCEPTS = _ruleset_us_gaap_concepts( "revenue", "bank_revenue", "interest_income_expense_net", "noninterest_income", extra=_EXTRA_REVENUE ); _COGS_CONCEPTS = _ruleset_us_gaap_concepts("cost_of_revenue", "insurance_claims"); _ASSETS_CONCEPTS = _ruleset_us_gaap_concepts("total_assets")
@dataclass(frozen=True)
class VendorFact: value: Decimal; period_end: date; concept: str; filed: date
@dataclass(frozen=True)
class Drift:
    ticker: str; field: str; mart_value: Decimal | None; vendor: VendorFact | None; cutoff: date; mart_period_end: date | None = None
    @property
    def agrees(self) -> bool:
        if self.vendor is None: return True
        if self.mart_value is None: return False
        return self.mart_value == self.vendor.value
    @property
    def staleness_years(self) -> int | None:
        if self.agrees or self.vendor is None or self.mart_period_end is None: return None
        return max(0, self.vendor.period_end.year - self.mart_period_end.year)
def _http_json(url: str, user_agent: str, key: FetchKey) -> dict[str, Any]:
    from data_engine.sources import gateway
    def fetch(identity: FetchKey) -> bytes: request = urllib.request.Request(url, headers={"User-Agent": user_agent}); _status, body = gateway.urlopen( "sec", "vendor-oracle", request, identity=identity, caller="quality.vendor_oracle", timeout=90, raise_for_status=True, ); return body
    return json.loads(gateway.fetch_once(key, fetch).decode())
def ticker_cik_index(user_agent: str) -> dict[str, int]: from data_engine.sources import sec; payload = _http_json(_TICKERS_URL, user_agent, sec.company_tickers_key()); return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()}
def company_facts(cik: int, user_agent: str) -> dict[str, Any]: from data_engine.sources import sec; return _http_json(_COMPANY_FACTS_URL.format(cik=cik), user_agent, sec.company_facts_key(cik))
def _annual_entries( facts: dict[str, Any], concept: str, unit: str, cutoff: date ) -> Iterable[tuple[date, Decimal, date]]:
    entries = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit) or []
    for entry in entries:
        filed_raw, end_raw, start_raw, value_raw = ( entry.get("filed"), entry.get("end"), entry.get("start"), entry.get("val"), )
        if not filed_raw or not end_raw or value_raw is None: continue
        try:
            filed, end = date.fromisoformat(filed_raw), date.fromisoformat(end_raw)
            if start_raw is not None and (end - date.fromisoformat(start_raw)).days < _ANNUAL_MINIMUM_DAYS: continue
            value = Decimal(str(value_raw))
        except (InvalidOperation, ValueError): continue
        if filed > cutoff: continue
        yield end, value, filed
def latest_across_variants( facts: dict[str, Any], concepts: Sequence[str], unit: str, cutoff: date ) -> VendorFact | None:
    best: VendorFact | None = None
    for concept in concepts:
        for period_end, value, filed in _annual_entries(facts, concept, unit, cutoff):
            if best is None or period_end > best.period_end: best = VendorFact(value=value, period_end=period_end, concept=concept, filed=filed)
    return best
def revenue(facts: dict[str, Any], cutoff: date) -> VendorFact | None: return latest_across_variants(facts, _REVENUE_CONCEPTS, "USD", cutoff)
def total_assets(facts: dict[str, Any], cutoff: date) -> VendorFact | None: return latest_across_variants(facts, _ASSETS_CONCEPTS, "USD", cutoff)
def gross_profit(facts: dict[str, Any], cutoff: date) -> VendorFact | None:
    direct = latest_across_variants(facts, ("GrossProfit",), "USD", cutoff); revenue_by_period = _all_periods(facts, _REVENUE_CONCEPTS, cutoff); cost_by_period = _all_periods(facts, _COGS_CONCEPTS, cutoff); shared = set(revenue_by_period) & set(cost_by_period); derived: VendorFact | None = None
    if shared: end = max(shared); (revenue_value, revenue_filed), (cost_value, cost_filed) = revenue_by_period[end], cost_by_period[end]; derived = VendorFact( value=revenue_value - cost_value, period_end=end, concept="Revenues-CostOfRevenue", filed=max(revenue_filed, cost_filed), )
    candidates = [item for item in (direct, derived) if item is not None]; return max(candidates, key=lambda item: item.period_end) if candidates else None
def period_reporting( facts: dict[str, Any], concepts: Sequence[str], value: Decimal | None, cutoff: date ) -> date | None:
    if value is None: return None
    found: date | None = None
    for concept in concepts:
        for period_end, candidate, _filed in _annual_entries(facts, concept, "USD", cutoff):
            if candidate == value and (found is None or period_end > found): found = period_end
    return found
def _all_periods(facts: dict[str, Any], concepts: Sequence[str], cutoff: date) -> dict[date, tuple[Decimal, date]]:
    merged: dict[date, tuple[Decimal, date]] = {}
    for concept in concepts:
        for period_end, value, filed in _annual_entries(facts, concept, "USD", cutoff): merged.setdefault(period_end, (value, filed))
    return merged
def mart_financial_facts(connection: Connection[Any]) -> tuple[tuple[str, Decimal | None, Decimal | None], ...]: rows = connection.execute( "\n        -- `limit 1` with no ordering picked an arbitrary universe's head, so the oracle\n        -- re-derived whichever pipeline the planner happened to return. The universe is part\n        -- of the governed key.\n        with head as (\n            select target_run_id from mart.current_pointer_head\n            where universe_id like %s\n            order by sequence desc limit 1\n        ),\n        payloads as (\n            select observation.subject_id, observation.semantic_type, payload.normalized_payload\n            from staging.capture_normalized_observations observation\n            join staging.capture_observation_payloads payload using (observation_id)\n            join staging.capture_observation_obligations usage using (observation_id)\n            join raw.capture_obligations obligation\n              on obligation.obligation_id = usage.capture_obligation_id\n            where obligation.run_id = (select target_run_id from head)\n        )\n        select identity.normalized_payload->>'ticker',\n               (financial.normalized_payload->>'revenue')::numeric,\n               (financial.normalized_payload->>'gross_profit')::numeric\n        from payloads identity\n        join payloads financial\n          on financial.subject_id = identity.subject_id and financial.semantic_type = 'financial-fact'\n        where identity.semantic_type = 'listing-identity'\n        order by 1\n        ", (id_prefix_like(universe_with_role("strategy")),), ).fetchall(); return tuple((str(row[0]).upper(), row[1], row[2]) for row in rows)
def compare(connection: Connection[Any], *, cutoff: date, user_agent: str) -> tuple[Drift, ...]:
    facts_rows = mart_financial_facts(connection)
    if not facts_rows: return ()
    index = ticker_cik_index(user_agent); drifts: list[Drift] = []; seen: dict[int, dict[str, Any]] = {}
    for ticker, mart_revenue, mart_gross_profit in facts_rows:
        cik = index.get(ticker.replace(".", "-"))
        if cik is None: continue
        if cik not in seen: seen[cik] = company_facts(cik, user_agent)
        facts = seen[cik]; drifts.append( Drift( ticker=ticker, field="revenue", mart_value=mart_revenue, vendor=latest_across_variants(facts, _REVENUE_CONCEPTS, "USD", cutoff), cutoff=cutoff, mart_period_end=period_reporting(facts, _REVENUE_CONCEPTS, mart_revenue, cutoff), ) ); drifts.append( Drift( ticker=ticker, field="gross_profit", mart_value=mart_gross_profit, vendor=gross_profit(facts, cutoff), cutoff=cutoff, mart_period_end=period_reporting(facts, ("GrossProfit",), mart_gross_profit, cutoff), ) )
    return tuple(drifts)
