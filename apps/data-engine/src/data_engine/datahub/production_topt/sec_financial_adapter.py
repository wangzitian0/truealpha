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

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from factors.production_topt import OperatingBranch
from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource

from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION

# Two kinds of concept list, and they must never be resolved the same way.
#
# SYNONYM lists hold tags for the SAME quantity that an issuer used at different times —
# `Revenues` and `RevenueFromContractWithCustomerExcludingAssessedTax` are both "total
# revenue", the second replaced the first at the ASC 606 transition. Merging these into one
# period-keyed series is correct and is what follows an issuer across the switch (#496).
#
# FALLBACK lists hold tags for DIFFERENT quantities, ordered by how well each stands in for
# the one we want. Merging those by period is wrong however recent the alternative is: it
# silently swaps in another number. Resolution stops at the first concept the issuer reports
# at all, so a stand-in is only reached when the exact quantity is absent entirely.
_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")  # synonyms
_COGS_CONCEPTS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices")  # synonyms
# FALLBACK: for a bank, plain `Revenues` is gross of interest expense, so subtracting
# noninterest expense from it is not pre-provision NET revenue. Only usable when the
# issuer publishes no net-of-interest total — never because it happens to be more recent.
_BANK_REVENUE_CONCEPTS = ("RevenuesNetOfInterestExpense", "Revenues")
_TOTAL_ASSETS = ("us-gaap", "Assets", "USD")
# SYNONYMS: both are shares *outstanding*, one on the cover page and one in the statements.
# Share counts live in `dei` for most large filers; a single us-gaap concept left
# ABBV/JNJ/LLY with no count at all (#496).
#
# `CommonStockSharesIssued` is deliberately NOT here. Issued includes treasury stock and is
# a different quantity — JNJ reports 3,119,843,000 issued against 2,409,898,597
# outstanding. Ranking it by period end would hand market cap a 29% error on whichever
# filing cycle it happened to carry the later date, with nothing in the payload disclosing
# the substitution.
_SHARES_CONCEPTS = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    # #496 LAST-RESORT: dual-class filers (META) tag point-in-time shares only
    # per class with dimensions, which the company-facts API drops entirely —
    # the annual weighted-average is the only whole-entity share count the API
    # carries. Preference order keeps it from ever shadowing a real
    # point-in-time figure (mapping v3).
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
)
# #496: the insurance operating numerator subtracts policyholder benefits/
# claims from revenue (owner-approved 2026-07-28) — the insurance analog of
# the bank PPNR proxy. Preference list, not synonyms (`_preferred_variant`):
# the entries measure different nettings and recency must not promote one.
_CLAIMS_CONCEPTS = (
    "PolicyholderBenefitsAndClaimsIncurredNet",
    "BenefitsLossesAndExpenses",
    "IncurredClaimsPropertyCasualtyAndLiability",
)
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
    # The fiscal periods the two headline figures describe. Carried into the normalized
    # payload because without them the warehouse cannot answer how old its own numbers
    # are: the seven-year-stale AAPL revenue was only detectable by re-deriving from the
    # vendor. With them, staleness is a plain SQL invariant (#429).
    operating_period_end: date | None = None
    revenue_period_end: date | None = None


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
    """One eligible XBRL fact: its value, the filing that made it knowable, and the
    fiscal period it describes.

    `period_end` is what distinguishes a current figure from an abandoned tag's last
    gasp, so it travels with the value rather than only being the key of the dict it
    came out of — a datum that has left that dict must still be comparable.
    """

    value: Decimal
    filed: date
    period_end: date


def annual_values_by_period_end(
    facts: dict[str, Any], taxonomy: str, concept: str, unit: str, cutoff: date
) -> dict[date, _Datum]:
    """Annual values of one XBRL concept knowable at the cutoff, keyed by period end.

    Only facts *filed* on or before the cutoff are eligible — a later filing is not
    knowable and would be look-ahead. Instant facts (no `start`) are kept as reported;
    shorter spans are quarterly and must never be compared with annual figures.

    One period end appears many times in company-facts — the original filing plus every
    later document that restated or simply re-reported it. The latest *filing* wins, never
    the entry that happens to sit last in the JSON array: selecting by array position makes
    restatement handling depend on the vendor's serialization order, which is the
    "never select the most recently inserted row" rule read backwards.
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
        existing = values.get(end)
        if existing is None or filed > existing.filed:
            values[end] = _Datum(value=value, filed=filed, period_end=end)
    return values


def _latest(values: dict[date, _Datum]) -> _Datum | None:
    return values[max(values)] if values else None


def _merge_variants(
    facts: dict[str, Any], concepts: Sequence[tuple[str, str]], unit: str, cutoff: date
) -> dict[date, _Datum]:
    """Every annual period any declared variant reports, as one series.

    An issuer's tagging is not stable across time: nearly every large filer moved off
    `Revenues` at the ASC 606 transition, and company-facts keeps the abandoned tag's
    history forever. Merging the variants into one period-keyed series before selecting
    is what lets the series follow the issuer across that switch. Earlier variants win a
    period they share, so declaration order still expresses preference — it just can no
    longer decide *recency*.
    """
    merged: dict[date, _Datum] = {}
    for taxonomy, concept in concepts:
        for end, datum in annual_values_by_period_end(facts, taxonomy, concept, unit, cutoff).items():
            merged.setdefault(end, datum)
    return merged


def _preferred_variant(
    facts: dict[str, Any], concepts: Sequence[tuple[str, str]], unit: str, cutoff: date
) -> dict[date, _Datum]:
    """The first concept the issuer reports at all — for lists whose entries differ in meaning.

    The counterpart to `_merge_variants`. Where variants are stand-ins rather than synonyms,
    recency must not promote one: a more recent number for a different quantity is still a
    different quantity. Falling back only when the preferred concept is entirely absent keeps
    the substitution rare, deliberate, and driven by availability rather than by dates.
    """
    for taxonomy, concept in concepts:
        values = annual_values_by_period_end(facts, taxonomy, concept, unit, cutoff)
        if values:
            return values
    return {}


def _latest_across_variants(facts: dict[str, Any], concepts: Sequence[str], unit: str, cutoff: date) -> _Datum | None:
    """The most recent annual value across ALL variants (us-gaap), not the first with data.

    The rule this replaces returned the first concept carrying any value, so an issuer
    that stopped using a tag stayed pinned to it: AAPL's mart revenue was its FY2018
    `Revenues` figure for seven years while `RevenueFromContractWithCustomer…` carried
    FY2025 (#496).
    """
    return _latest(_merge_variants(facts, tuple(("us-gaap", concept) for concept in concepts), unit, cutoff))


def _difference_at_shared_period(base: dict[date, _Datum], subtracted: dict[date, _Datum]) -> _Datum | None:
    """`base - subtracted` at their latest *shared* annual period end.

    Two concepts reported for different periods are not comparable, so only a shared
    period end resolves; the difference is knowable once both filings are.

    Both sides arrive already resolved, because how each side resolves is a property of
    what its concepts MEAN — synonyms merge, stand-ins do not — and that decision belongs
    to the caller that knows the semantics, not to the arithmetic here.
    """
    shared = set(base) & set(subtracted)
    if not shared:
        return None
    end = max(shared)
    return _Datum(
        value=base[end].value - subtracted[end].value,
        filed=max(base[end].filed, subtracted[end].filed),
        period_end=end,
    )


def _period_matched_difference(
    facts: dict[str, Any], minuend: Sequence[str], subtrahend: Sequence[str], cutoff: date
) -> _Datum | None:
    """`minuend - subtrahend` where BOTH sides are synonym lists, so both merge.

    Iterating variant pairs and returning the first that shares any period could pair a
    legacy revenue tag with a current cost tag purely because that pair was reached first.
    """
    return _difference_at_shared_period(
        _merge_variants(facts, tuple(("us-gaap", concept) for concept in minuend), "USD", cutoff),
        _merge_variants(facts, tuple(("us-gaap", concept) for concept in subtrahend), "USD", cutoff),
    )


def gross_profit(facts: dict[str, Any], cutoff: date) -> _Datum | None:
    """Reported `GrossProfit` or revenue minus cost of revenue — whichever covers the later period.

    "Reported wins unconditionally" is not safe: Amazon last tagged `GrossProfit` for
    FY2009 and mart carried that figure against FY2025 headcount, which is what made its
    reported GPPE negative (#496). A directly reported figure is still preferred when the
    two cover the *same* period — it is the issuer's own assertion rather than our
    arithmetic — but it cannot outrank a more recent one.
    """
    direct = _latest(annual_values_by_period_end(facts, "us-gaap", "GrossProfit", "USD", cutoff))
    derived = _period_matched_difference(facts, _REVENUE_CONCEPTS, _COGS_CONCEPTS, cutoff)
    candidates = [datum for datum in (direct, derived) if datum is not None]
    if candidates:
        return max(candidates, key=lambda datum: datum.period_end)
    if _files_no_cogs_concepts(facts):
        # #496 owner decision (2026-07-28): an issuer whose company-facts carry
        # NO GrossProfit and NO COGS-family concept AT ALL (payment networks,
        # integrated oil majors) uses revenue as the gross-profit proxy —
        # their cost-of-revenue is ~zero or structurally unreported, the bias
        # is small and its direction known. Concept-level absence only: a mere
        # period mismatch on a real COGS filer still resolves to None.
        return _latest(_merge_variants(facts, tuple(("us-gaap", c) for c in _REVENUE_CONCEPTS), "USD", cutoff))
    return None


def _files_no_cogs_concepts(facts: dict[str, Any]) -> bool:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    return "GrossProfit" not in gaap and all(concept not in gaap for concept in _COGS_CONCEPTS)


def insurance_pre_claims_profit(facts: dict[str, Any], cutoff: date) -> _Datum | None:
    """Insurance operating numerator: revenue minus policyholder benefits/claims (#496).

    Mirrors `pre_provision_profit` in shape, with one extra guard: the shared
    period must be the revenue series' LATEST period. Berkshire's only
    API-visible claims concept stops in 2016 (segment-dimension facts are
    dropped by company-facts); a 2016 difference silently paired with 2025
    headcount is the Amazon-2009 GrossProfit lesson all over again, so a
    stale claims series resolves to None and the factor surfaces
    `missing_gross_profit` honestly — never a silent revenue proxy: for an
    insurer, claims ARE the cost of revenue.
    """
    revenue = _merge_variants(facts, tuple(("us-gaap", c) for c in _REVENUE_CONCEPTS), "USD", cutoff)
    claims = _preferred_variant(facts, tuple(("us-gaap", c) for c in _CLAIMS_CONCEPTS), "USD", cutoff)
    datum = _difference_at_shared_period(revenue, claims)
    if datum is None or datum.period_end != max(revenue):
        return None
    return datum


def pre_provision_profit(facts: dict[str, Any], cutoff: date) -> _Datum | None:
    """Bank pre-provision net revenue: net revenue minus noninterest expense (#59).

    The revenue side is a FALLBACK list, so it resolves by preference rather than by
    recency: `Revenues` for a bank is gross of interest expense, and subtracting
    noninterest expense from it yields something that is not pre-provision net revenue at
    all. It is reached only when the issuer publishes no net-of-interest total — never
    because it carries a later period end.
    """
    return _difference_at_shared_period(
        _preferred_variant(facts, tuple(("us-gaap", concept) for concept in _BANK_REVENUE_CONCEPTS), "USD", cutoff),
        annual_values_by_period_end(facts, "us-gaap", "NoninterestExpense", "USD", cutoff),
    )


def build_bundle(
    facts: dict[str, Any], cutoff: date, branch: OperatingBranch, *, raw_bytes: bytes | None = None
) -> FinancialFactsBundle:
    """Extract the PIT financial-fact bundle from a company-facts payload.

    A payload with no eligible fact is not a failure: the source honestly asserts
    nothing for this issuer at this cutoff, so every field resolves to `None` and the
    factor surfaces the gap (`missing_gross_profit`, …) instead of the capture
    inventing one. A source that could not be *reached* raises instead — that is a
    retryable failure, never a null-filled fact.
    """
    assets = _latest(annual_values_by_period_end(facts, *_TOTAL_ASSETS, cutoff))
    shares = _latest(_merge_variants(facts, _SHARES_CONCEPTS, "shares", cutoff))
    revenue = _latest_across_variants(facts, _REVENUE_CONCEPTS, "USD", cutoff)
    # large_model_value_v0 applies one uniform capital-adjusted formula to every issuer;
    # the branch only decides WHICH versioned extraction asserts the operating numerator:
    # banks use the pre-provision proxy, insurers revenue-minus-claims (#496), everyone
    # else reported/derived gross profit (with the no-COGS revenue proxy inside).
    if branch is OperatingBranch.FINANCIAL:
        profit = pre_provision_profit(facts, cutoff)
    elif branch is OperatingBranch.INSURANCE:
        profit = insurance_pre_claims_profit(facts, cutoff)
    else:
        profit = gross_profit(facts, cutoff)
    resolved = [datum for datum in (profit, assets, shares, revenue) if datum is not None]
    knowable = max((datum.filed for datum in resolved), default=None)
    return FinancialFactsBundle(
        gross_profit=_v(profit),
        total_assets=_v(assets),
        shares_outstanding=_v(shares),
        revenue=_v(revenue),
        pre_provision_profit=_v(profit) if branch is OperatingBranch.FINANCIAL else None,
        # The vendor's own document when the caller has it. The fallback re-serialization
        # is for callers that only ever hold a parsed dict (tests); its digest describes our
        # rendering, not SEC's, so it must never be what a live capture lands.
        raw_bytes=raw_bytes
        if raw_bytes is not None
        else json.dumps(facts, sort_keys=True, separators=(",", ":")).encode(),
        knowable_at=None if knowable is None else datetime.combine(knowable, datetime.min.time(), tzinfo=UTC),
        operating_period_end=None if profit is None else profit.period_end,
        revenue_period_end=None if revenue is None else revenue.period_end,
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
            "operating_period_end": _d(bundle.operating_period_end),
            "revenue_period_end": _d(bundle.revenue_period_end),
        }
        return FetchSuccess(
            raw=RawResponse(
                body=bundle.raw_bytes,
                source=DataSource.SEC,
                record_id=f"companyfacts:CIK{target.cik:010d}",
            ),
            normalized_sha256=canonical_sha256(payload),
            confidence=_confidence(payload),
            valid_from=knowable_at.date(),
            transaction_time=knowable_at,
            record=NormalizedRecord(payload=payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION),
        )


def _confidence(payload: dict[str, str | None]) -> Decimal:
    """Per-source-class confidence prior (#207/#404); the calibrated formula is #337."""
    present = sum(payload.get(field) is not None for field in ("gross_profit", "total_assets", "shares_outstanding"))
    return {3: Decimal("0.92"), 2: Decimal("0.80"), 1: Decimal("0.65")}.get(present, Decimal("0.50"))


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def sec_financial_fetcher(cik: int, cutoff: date, branch: OperatingBranch) -> FinancialFactsBundle:
    """Default fetcher: the real SEC company-facts client, parsed point-in-time.

    Imported lazily so the adapter and its tests carry no network dependency.
    """

    import httpx

    from data_engine.sources import sec

    try:
        body, facts = sec.fetch_company_facts_response(cik)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    return build_bundle(facts, cutoff, branch, raw_bytes=body)
