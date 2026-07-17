"""SEC company-facts financial-fact source adapter (Phase 3d, ADR A1 / #171).

Implements `SourceFetchPort` for the `financial-fact` semantic. It resolves each work item to
(CIK, cutoff), pulls the SEC XBRL company-facts through an injected fetcher (the real SEC
client by default; a fake in tests), and extracts point-in-time `gross_profit`, `total_assets`,
and `shares_outstanding` — only facts *filed on or before the cutoff*, most-recent period,
parsed as Decimal. SEC XBRL tags/units vary across issuers, so a missing concept resolves to
`None` (the factor decides availability) and an all-missing bundle to FIELD_UNAVAILABLE.

`headcount` is not a reliable XBRL concept; it is supplied by the #70 extraction plane and
merged into the financial-fact record in a later slice, not here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem

from data_engine.datahub.production_topt.executor import FetchFailure, FetchOutcome, FetchSuccess

# The us-gaap / dei concepts and units each normalized field is drawn from.
_GROSS_PROFIT = ("us-gaap", "GrossProfit", "USD")
_TOTAL_ASSETS = ("us-gaap", "Assets", "USD")
_SHARES = ("dei", "EntityCommonStockSharesOutstanding", "shares")


@dataclass(frozen=True)
class FinancialFactsBundle:
    gross_profit: Decimal | None
    total_assets: Decimal | None
    shares_outstanding: Decimal | None
    raw_bytes: bytes
    knowable_at: datetime


@dataclass(frozen=True)
class SecTarget:
    cik: int
    cutoff: date


FinancialFactsFetcher = Callable[[int, date], FinancialFactsBundle | None]


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


def pit_concept_value(
    facts: dict[str, Any], taxonomy: str, concept: str, unit: str, cutoff: date
) -> tuple[Decimal, date] | None:
    """The most-recent value of one XBRL concept filed on or before the cutoff, with its
    filing (knowable) date. Returns None when no eligible datum exists."""
    entries = facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit)
    if not entries:
        return None
    eligible: list[tuple[date, date, Decimal]] = []
    for entry in entries:
        filed_raw, end_raw, val = entry.get("filed"), entry.get("end"), entry.get("val")
        if filed_raw is None or end_raw is None or val is None:
            continue
        filed = date.fromisoformat(filed_raw)
        if filed > cutoff:
            continue  # not knowable at the cutoff — excluded, never look-ahead
        try:
            eligible.append((date.fromisoformat(end_raw), filed, Decimal(str(val))))
        except (InvalidOperation, ValueError):
            continue
    if not eligible:
        return None
    end, filed, value = max(eligible, key=lambda item: (item[0], item[1]))
    return value, filed


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
            bundle = self._fetcher(target.cik, target.cutoff)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)
        if bundle is None or (
            bundle.gross_profit is None and bundle.total_assets is None and bundle.shares_outstanding is None
        ):
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if bundle.knowable_at.date() > target.cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        # Enrich with the #70 headcount extraction, if any, respecting point-in-time.
        headcount: Decimal | None = None
        knowable_at = bundle.knowable_at
        if self._headcount_extractor is not None:
            fact = self._headcount_extractor(target.cik, target.cutoff)
            if fact is not None and fact.knowable_at.date() <= target.cutoff:
                headcount = fact.value
                knowable_at = max(knowable_at, fact.knowable_at)
        raw_sha256 = hashlib.sha256(bundle.raw_bytes).hexdigest()
        normalized_sha256 = canonical_sha256(
            {
                "semantic_type": "financial-fact",
                "cik": target.cik,
                "gross_profit": _s(bundle.gross_profit),
                "total_assets": _s(bundle.total_assets),
                "shares_outstanding": _s(bundle.shares_outstanding),
                "headcount": _s(headcount),
            }
        )
        return FetchSuccess(
            raw_sha256=raw_sha256,
            object_uri=f"s3://truealpha-raw/sec/companyfacts/CIK{target.cik:010d}.json",
            normalized_sha256=normalized_sha256,
            confidence=Decimal("0.95"),
            valid_from=knowable_at.date(),
            transaction_time=knowable_at,
        )


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def sec_financial_fetcher(cik: int, cutoff: date) -> FinancialFactsBundle | None:
    """Default fetcher: the real SEC company-facts client, parsed point-in-time.

    Imported lazily so the adapter and its tests carry no network dependency.
    """

    import httpx

    from data_engine.sources import sec

    try:
        facts = sec.fetch_company_facts(cik)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    return build_bundle(facts, cutoff)


def build_bundle(facts: dict[str, Any], cutoff: date) -> FinancialFactsBundle | None:
    """Extract the PIT financial-fact bundle from a company-facts payload."""
    import json

    fields = {
        "gross_profit": pit_concept_value(facts, *_GROSS_PROFIT, cutoff),
        "total_assets": pit_concept_value(facts, *_TOTAL_ASSETS, cutoff),
        "shares_outstanding": pit_concept_value(facts, *_SHARES, cutoff),
    }
    present = {name: hit for name, hit in fields.items() if hit is not None}
    if not present:
        return None
    knowable = max(filed for _, filed in present.values())
    knowable_at = datetime.combine(knowable, datetime.min.time(), tzinfo=UTC)
    return FinancialFactsBundle(
        gross_profit=_value(fields["gross_profit"]),
        total_assets=_value(fields["total_assets"]),
        shares_outstanding=_value(fields["shares_outstanding"]),
        raw_bytes=json.dumps(facts, sort_keys=True, separators=(",", ":")).encode(),
        knowable_at=knowable_at,
    )


def _value(hit: tuple[Decimal, date] | None) -> Decimal | None:
    return None if hit is None else hit[0]
