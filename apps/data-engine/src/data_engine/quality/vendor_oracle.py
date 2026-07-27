"""Re-derive what mart asserts, straight from the vendor, and compare (#429).

An invariant (`quality/invariants.py`) catches a number that contradicts itself. It cannot
catch a number that is perfectly self-consistent and simply not the current one — AAPL's
FY2018 revenue satisfies every invariant there is. Only an independent re-derivation from
the source catches that, so this module fetches SEC company-facts live at check time and
compares.

## Why this deliberately reimplements the extraction

Nothing here imports `datahub.production_topt.sec_financial_adapter`, and that is the
entire point: an oracle that calls the code under test inherits its bugs and reports green
on exactly the data it exists to catch. The audit that motivated this found the adapter
selecting the *first* concept variant carrying any value rather than the most recent one,
which pinned AAPL to a tag abandoned in 2018. Re-deriving through the same function would
have reproduced that answer and agreed with itself.

So the rule for this file: **resolve values by an independent route, and let the two
disagree.** It reads more concept variants than the adapter declares (a vendor's real
tagging is wider than any mapping we froze), and it selects across all of them by latest
period end rather than by declaration order.

A checked-in expected value would not do either: a constant is authored by this repository
too, so it drifts silently the moment the issuer files again. The right-hand side has to
come over the network at check time.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import Connection

_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC fair use is ~10 req/s; a check run is small and has no reason to approach it.
_PACE_SECONDS = 0.25
# An annual period. 350 days absorbs 52/53-week fiscal calendars.
_ANNUAL_MINIMUM_DAYS = 350

# Wider than the adapter's frozen mapping on purpose — see the module docstring. An
# oracle that only knows the tags we already declared cannot reveal a tag we missed.
_REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenuesNetOfInterestExpense",
)
_COGS_CONCEPTS = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
    "CostOfSales",
)


@dataclass(frozen=True)
class VendorFact:
    """One independently derived annual figure and the period it belongs to."""

    value: Decimal
    period_end: date
    concept: str


@dataclass(frozen=True)
class Drift:
    """What mart holds for one field versus what the vendor says today.

    `mart_period_end` is recovered by finding which period the vendor reported mart's
    exact value for. That indirection is necessary because the normalized payload does
    not record the fiscal period a figure came from — so the warehouse alone cannot
    answer "how old is this number", and the drift is invisible from inside.
    """

    ticker: str
    field: str
    mart_value: Decimal | None
    vendor: VendorFact | None
    cutoff: date
    mart_period_end: date | None = None

    @property
    def agrees(self) -> bool:
        if self.vendor is None:
            return True  # the vendor asserts nothing; mart having a gap is not drift
        if self.mart_value is None:
            return False
        return self.mart_value == self.vendor.value

    @property
    def staleness_years(self) -> int | None:
        """How many years behind the vendor's current period mart's figure actually is.

        Distinguishes the two defects that look identical in a diff: a restatement (same
        period, revised value) and a stale tag (a period years back). Only resolvable
        when the value was matched to a vendor period.
        """
        if self.agrees or self.vendor is None or self.mart_period_end is None:
            return None
        return max(0, self.vendor.period_end.year - self.mart_period_end.year)


def _http_json(url: str, user_agent: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - fixed SEC hosts
        return json.loads(response.read().decode())


def ticker_cik_index(user_agent: str) -> dict[str, int]:
    payload = _http_json(_TICKERS_URL, user_agent)
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()}


def company_facts(cik: int, user_agent: str) -> dict[str, Any]:
    return _http_json(_COMPANY_FACTS_URL.format(cik=cik), user_agent)


def _annual_entries(facts: dict[str, Any], concept: str, unit: str, cutoff: date) -> Iterable[tuple[date, Decimal]]:
    """Annual (period_end, value) pairs for one concept that were filed by `cutoff`."""
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
        if filed > cutoff:
            continue  # not knowable at the cutoff
        yield end, value


def latest_across_variants(
    facts: dict[str, Any], concepts: Sequence[str], unit: str, cutoff: date
) -> VendorFact | None:
    """The most recent annual value across ALL variants, not the first variant with data.

    This one line is the difference the audit turned on. Selecting per-variant and
    returning the first non-empty result locks an issuer onto whichever tag it used to
    file under; comparing period ends across every variant follows the issuer when it
    switches tags (as nearly every large filer did at the ASC 606 transition).
    """
    best: VendorFact | None = None
    for concept in concepts:
        for period_end, value in _annual_entries(facts, concept, unit, cutoff):
            if best is None or period_end > best.period_end:
                best = VendorFact(value=value, period_end=period_end, concept=concept)
    return best


def gross_profit(facts: dict[str, Any], cutoff: date) -> VendorFact | None:
    """Reported gross profit, or revenue minus cost of revenue — whichever is more recent.

    The adapter prefers a directly reported `GrossProfit` unconditionally. Amazon last
    tagged it for FY2009 while its FY2025 difference is computable from tags that are
    present, so "reported wins" is not a safe rule; the later period wins here.
    """
    direct = latest_across_variants(facts, ("GrossProfit",), "USD", cutoff)
    revenue_by_period = dict(_all_periods(facts, _REVENUE_CONCEPTS, cutoff))
    cost_by_period = dict(_all_periods(facts, _COGS_CONCEPTS, cutoff))
    shared = set(revenue_by_period) & set(cost_by_period)
    derived: VendorFact | None = None
    if shared:
        end = max(shared)
        derived = VendorFact(
            value=revenue_by_period[end] - cost_by_period[end],
            period_end=end,
            concept="Revenues-CostOfRevenue",
        )
    candidates = [item for item in (direct, derived) if item is not None]
    return max(candidates, key=lambda item: item.period_end) if candidates else None


def period_reporting(
    facts: dict[str, Any], concepts: Sequence[str], value: Decimal | None, cutoff: date
) -> date | None:
    """The latest period any variant reported `value` for — i.e. where mart's figure came from.

    A figure that matches no period at all is a computed or corrupted value rather than a
    stale selection, and stays `None` so the report does not claim a staleness it cannot
    substantiate.
    """
    if value is None:
        return None
    found: date | None = None
    for concept in concepts:
        for period_end, candidate in _annual_entries(facts, concept, "USD", cutoff):
            if candidate == value and (found is None or period_end > found):
                found = period_end
    return found


def _all_periods(facts: dict[str, Any], concepts: Sequence[str], cutoff: date) -> Iterable[tuple[date, Decimal]]:
    """Every annual period any variant reports, latest variant winning a shared period."""
    merged: dict[date, Decimal] = {}
    for concept in concepts:
        for period_end, value in _annual_entries(facts, concept, "USD", cutoff):
            merged.setdefault(period_end, value)
    return merged.items()


def mart_financial_facts(connection: Connection[Any]) -> tuple[tuple[str, Decimal | None, Decimal | None], ...]:
    """(ticker, revenue, gross_profit) for the run the governed pointer currently heads."""
    rows = connection.execute(
        """
        with head as (select target_run_id from mart.current_pointer_head limit 1),
        payloads as (
            select observation.subject_id, observation.semantic_type, payload.normalized_payload
            from staging.capture_normalized_observations observation
            join staging.capture_observation_payloads payload using (observation_id)
            join staging.capture_observation_obligations usage using (observation_id)
            join raw.capture_obligations obligation
              on obligation.obligation_id = usage.capture_obligation_id
            where obligation.run_id = (select target_run_id from head)
        )
        select identity.normalized_payload->>'ticker',
               (financial.normalized_payload->>'revenue')::numeric,
               (financial.normalized_payload->>'gross_profit')::numeric
        from payloads identity
        join payloads financial
          on financial.subject_id = identity.subject_id and financial.semantic_type = 'financial-fact'
        where identity.semantic_type = 'listing-identity'
        order by 1
        """
    ).fetchall()
    return tuple((str(row[0]).upper(), row[1], row[2]) for row in rows)


def compare(connection: Connection[Any], *, cutoff: date, user_agent: str) -> tuple[Drift, ...]:
    """Re-derive revenue and gross profit from SEC for the governed run, and report drift."""
    facts_rows = mart_financial_facts(connection)
    if not facts_rows:
        return ()
    index = ticker_cik_index(user_agent)
    time.sleep(_PACE_SECONDS)
    drifts: list[Drift] = []
    seen: dict[int, dict[str, Any]] = {}
    for ticker, mart_revenue, mart_gross_profit in facts_rows:
        cik = index.get(ticker.replace(".", "-"))
        if cik is None:
            continue
        if cik not in seen:  # dual-class listings share one CIK; one fetch covers both
            seen[cik] = company_facts(cik, user_agent)
            time.sleep(_PACE_SECONDS)
        facts = seen[cik]
        drifts.append(
            Drift(
                ticker=ticker,
                field="revenue",
                mart_value=mart_revenue,
                vendor=latest_across_variants(facts, _REVENUE_CONCEPTS, "USD", cutoff),
                cutoff=cutoff,
                mart_period_end=period_reporting(facts, _REVENUE_CONCEPTS, mart_revenue, cutoff),
            )
        )
        drifts.append(
            Drift(
                ticker=ticker,
                field="gross_profit",
                mart_value=mart_gross_profit,
                vendor=gross_profit(facts, cutoff),
                cutoff=cutoff,
                mart_period_end=period_reporting(facts, ("GrossProfit",), mart_gross_profit, cutoff),
            )
        )
    return tuple(drifts)
