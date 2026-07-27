"""SEC company-facts financial-fact source adapter (Phase 3d, ADR A1 / #171).

Implements `SourceFetchPort` for the `financial-fact` semantic. It resolves each work item
to (CIK, cutoff, identity, operating branch), pulls the SEC XBRL company-facts through an
injected fetcher (the real SEC client by default; a fake in tests), and extracts the
point-in-time annual figures the TOPT core consumes — only facts *filed on or before the
cutoff*, most recent annual period, parsed as Decimal.

SEC XBRL tags and units vary across issuers, so each field is resolved through a declared
concept-variant list and a missing concept resolves to `None` — the factor decides
availability and names the gap. "The source publishes no such fact" is a captured record
with null fields, not a failure; only a source that could not be *reached* fails, and it
fails with a retryable reason code rather than a null-filled fact.

The operating branch (#59/#420/#451) is *not* decided here from a ticker allowlist: it is
resolved from issuer registry metadata by the composition root and arrives on the target.
A depository-institution issuer reports no gross profit, so its operating numerator is the
pre-provision net revenue proxy; without that, every bank lands with `gross_profit=None`
and the evaluator silently excludes it as `missing_gross_profit` instead of scoring it.

`headcount` is not a reliable XBRL concept; it arrives through the `HeadcountExtractor`
port (#70), never as a branch in generic capture code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from factors.production_topt import OperatingBranch
from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
)

PARSER_VERSION = "production-topt-live-parser:v1"
MAPPING_VERSION = "production-topt-live-map:v1"

# The us-gaap concepts each normalized field is drawn from, in resolution order.
# SEC XBRL heterogeneity: issuers report the same economics under different tags.
_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
_COGS_CONCEPTS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices")
_BANK_REVENUE_CONCEPTS = ("RevenuesNetOfInterestExpense", "Revenues")
_TOTAL_ASSETS = ("us-gaap", "Assets", "USD")
_SHARES = ("us-gaap", "CommonStockSharesOutstanding", "shares")
# An annual period: shorter spans are quarterly facts that must not be compared with
# annual ones. 350 days absorbs 52/53-week fiscal calendars.
_ANNUAL_MINIMUM_DAYS = 350


@dataclass(frozen=True)
class FinancialFactsBundle:
    """The PIT annual figures one company-facts payload yields at a cutoff."""

    gross_profit: Decimal | None
    total_assets: Decimal | None
    shares_outstanding: Decimal | None
    revenue: Decimal | None
    pre_provision_profit: Decimal | None
    raw_bytes: bytes
    # None when the payload yielded no eligible fact at all — the issuer's company-facts
    # document exists but asserts nothing this cutoff can use (a real state: XOM's
    # post-reorganization CIK publishes no us-gaap taxonomy yet).
    knowable_at: datetime | None


@dataclass(frozen=True)
class SecTarget:
    """The resolved fetch coordinates for one financial-fact work item."""

    cik: int
    cutoff: date
    issuer_id: str
    instrument_id: str
    listing_id: str
    operating_branch: OperatingBranch
    currency: str = "USD"


FinancialFactsFetcher = Callable[[int, date, OperatingBranch], FinancialFactsBundle | None]


@dataclass(frozen=True)
class HeadcountFact:
    """A point-in-time employee headcount extracted from filing text (#70).

    Employee headcount is not a reliable XBRL concept, so it comes from the #70 extraction
    plane (append-only, evidence-spanned) rather than company-facts. Here it enriches the
    financial-fact record; SEC company-facts remains the primary raw source.
    """

    value: Decimal
    knowable_at: datetime


# (cik, cutoff) -> the extracted headcount fact, or None when no filing yields one.
HeadcountExtractor = Callable[[int, date], HeadcountFact | None]


class SourceUnavailableError(Exception):
    """A transient SEC failure the executor should retry."""


@dataclass(frozen=True)
class _Datum:
    """One eligible XBRL fact: its value and the filing date that made it knowable."""

    value: Decimal
    filed: date


def annual_values_by_period_end(
    facts: dict[str, Any], taxonomy: str, concept: str, unit: str, cutoff: date
) -> dict[date, _Datum]:
    """Annual values of one XBRL concept knowable at the cutoff, keyed by period end.

    Only facts *filed* on or before the cutoff are eligible — a later filing is not
    knowable and would be look-ahead. Instant facts (no `start`) are kept as reported;
    shorter spans are quarterly and must never be compared with annual figures.
    """
    entries = facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit)
    if not entries:
        return {}
    values: dict[date, _Datum] = {}
    for entry in entries:
        filed_raw, end_raw, start_raw, val = (
            entry.get("filed"),
            entry.get("end"),
            entry.get("start"),
            entry.get("val"),
        )
        if filed_raw is None or end_raw is None or val is None:
            continue
        try:
            filed, end = date.fromisoformat(filed_raw), date.fromisoformat(end_raw)
            if start_raw is not None and (end - date.fromisoformat(start_raw)).days < _ANNUAL_MINIMUM_DAYS:
                continue
            value = Decimal(str(val))
        except (InvalidOperation, ValueError):
            continue
        if filed > cutoff:
            continue
        values[end] = _Datum(value=value, filed=filed)
    return values


def _latest(values: dict[date, _Datum]) -> _Datum | None:
    return values[max(values)] if values else None


def _first_available(facts: dict[str, Any], concepts: Sequence[str], unit: str, cutoff: date) -> _Datum | None:
    for concept in concepts:
        datum = _latest(annual_values_by_period_end(facts, "us-gaap", concept, unit, cutoff))
        if datum is not None:
            return datum
    return None


def _period_matched_difference(
    facts: dict[str, Any], minuend: Sequence[str], subtrahend: Sequence[str], cutoff: date
) -> _Datum | None:
    """`minuend - subtrahend` at their latest *shared* annual period end.

    Two concepts reported for different periods are not comparable, so only a shared
    period end resolves; the difference is knowable once both filings are.
    """
    for subtrahend_concept in subtrahend:
        subtracted = annual_values_by_period_end(facts, "us-gaap", subtrahend_concept, "USD", cutoff)
        if not subtracted:
            continue
        for minuend_concept in minuend:
            base = annual_values_by_period_end(facts, "us-gaap", minuend_concept, "USD", cutoff)
            shared = set(base) & set(subtracted)
            if shared:
                end = max(shared)
                return _Datum(
                    value=base[end].value - subtracted[end].value,
                    filed=max(base[end].filed, subtracted[end].filed),
                )
    return None


def gross_profit(facts: dict[str, Any], cutoff: date) -> _Datum | None:
    """Reported `GrossProfit`, or revenue minus cost of revenue over a shared annual period."""
    direct = _latest(annual_values_by_period_end(facts, "us-gaap", "GrossProfit", "USD", cutoff))
    if direct is not None:
        return direct
    return _period_matched_difference(facts, _REVENUE_CONCEPTS, _COGS_CONCEPTS, cutoff)


def pre_provision_profit(facts: dict[str, Any], cutoff: date) -> _Datum | None:
    """Bank pre-provision net revenue: net revenue minus noninterest expense (#59)."""
    return _period_matched_difference(facts, _BANK_REVENUE_CONCEPTS, ("NoninterestExpense",), cutoff)


def build_bundle(facts: dict[str, Any], cutoff: date, branch: OperatingBranch) -> FinancialFactsBundle:
    """Extract the PIT financial-fact bundle from a company-facts payload.

    A payload with no eligible fact is not a failure: the source honestly asserts
    nothing for this issuer at this cutoff, so every field resolves to `None` and the
    factor surfaces the gap (`missing_gross_profit`, …) instead of the capture
    inventing one. A source that could not be *reached* raises instead — that is a
    retryable failure, never a null-filled fact.
    """
    assets = _latest(annual_values_by_period_end(facts, *_TOTAL_ASSETS, cutoff))
    shares = _latest(annual_values_by_period_end(facts, *_SHARES, cutoff))
    revenue = _first_available(facts, _REVENUE_CONCEPTS, "USD", cutoff)
    ppnr = pre_provision_profit(facts, cutoff) if branch is OperatingBranch.FINANCIAL else None
    # large_model_value_v0 applies one uniform capital-adjusted formula to every issuer,
    # financial branch included: a bank's operating numerator is the pre-provision-profit
    # proxy, not a reported gross profit it never files.
    profit = ppnr if branch is OperatingBranch.FINANCIAL else gross_profit(facts, cutoff)
    resolved = [datum for datum in (profit, assets, shares, revenue) if datum is not None]
    knowable = max((datum.filed for datum in resolved), default=None)
    return FinancialFactsBundle(
        gross_profit=_v(profit),
        total_assets=_v(assets),
        shares_outstanding=_v(shares),
        revenue=_v(revenue),
        pre_provision_profit=_v(ppnr),
        raw_bytes=json.dumps(facts, sort_keys=True, separators=(",", ":")).encode(),
        knowable_at=None if knowable is None else datetime.combine(knowable, datetime.min.time(), tzinfo=UTC),
    )


def _v(datum: _Datum | None) -> Decimal | None:
    return None if datum is None else datum.value


class SecFinancialFactAdapter:
    """`SourceFetchPort` for financial-fact, backed by an injected company-facts fetcher."""

    def __init__(
        self,
        targets: dict[str, SecTarget],
        fetcher: FinancialFactsFetcher,
        *,
        headcount_extractor: HeadcountExtractor | None = None,
    ) -> None:
        self._targets = targets
        self._fetcher = fetcher
        self._headcount_extractor = headcount_extractor

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        target = self._targets.get(work_item.work_item_id)
        if target is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        try:
            bundle = self._fetcher(target.cik, target.cutoff, target.operating_branch)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)
        if bundle is None:
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if bundle.knowable_at is not None and bundle.knowable_at.date() > target.cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        # Enrich with the #70 headcount extraction, if any, respecting point-in-time.
        headcount: Decimal | None = None
        # A payload that resolved nothing is knowable exactly at the cutoff: what it
        # asserts is "this source has no such fact yet", not a dated figure.
        knowable_at = bundle.knowable_at or datetime.combine(target.cutoff, datetime.min.time(), tzinfo=UTC)
        if self._headcount_extractor is not None:
            fact = self._headcount_extractor(target.cik, target.cutoff)
            if fact is not None and fact.knowable_at.date() <= target.cutoff:
                headcount = fact.value
                knowable_at = max(knowable_at, fact.knowable_at)
        payload = {
            "issuer_id": target.issuer_id,
            "instrument_id": target.instrument_id,
            "listing_id": target.listing_id,
            "operating_branch": target.operating_branch.value,
            "currency": target.currency,
            "gross_profit": _s(bundle.gross_profit),
            "total_assets": _s(bundle.total_assets),
            "headcount": _s(headcount),
            "revenue": _s(bundle.revenue),
            "shares_outstanding": _s(bundle.shares_outstanding),
            "pre_provision_profit": _s(bundle.pre_provision_profit),
        }
        return FetchSuccess(
            raw_sha256=hashlib.sha256(bundle.raw_bytes).hexdigest(),
            object_uri=f"s3://truealpha-raw/sec/companyfacts/CIK{target.cik:010d}.json",
            normalized_sha256=canonical_sha256(payload),
            confidence=_confidence(payload),
            valid_from=knowable_at.date(),
            transaction_time=knowable_at,
            record=NormalizedRecord(payload=payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION),
            raw_byte_length=len(bundle.raw_bytes),
        )


def _confidence(payload: dict[str, str | None]) -> Decimal:
    """Per-source-class confidence prior (#207/#404); the calibrated formula is #337."""
    present = sum(payload.get(field) is not None for field in ("gross_profit", "total_assets", "shares_outstanding"))
    return {3: Decimal("0.92"), 2: Decimal("0.80"), 1: Decimal("0.65")}.get(present, Decimal("0.50"))


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def sec_financial_fetcher(cik: int, cutoff: date, branch: OperatingBranch) -> FinancialFactsBundle:
    """Default fetcher: the real SEC company-facts client, parsed point-in-time.

    Imported lazily so the adapter and its tests carry no network dependency.
    """

    import httpx

    from data_engine.sources import sec

    try:
        facts = sec.fetch_company_facts(cik)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    return build_bundle(facts, cutoff, branch)
