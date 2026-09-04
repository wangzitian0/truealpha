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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from factors.production_topt import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset, ConceptRef, ResolutionKind
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION

# Concept lists are NOT here any more: they are a published, content-addressed ruleset
# (`concept_mapping.DEFAULT_RULESET`, superseded at runtime by the governed pointer), so
# a corrected mapping is an insert rather than a deploy. Keeping a second copy in code
# would recreate exactly the drift the ruleset removes.
# An annual period: shorter spans are quarterly facts that must not be compared with
# annual ones. 350 days absorbs 52/53-week fiscal calendars.
_ANNUAL_MINIMUM_DAYS = 350

# How far before the cutoff a share count may have been measured and still be used.
#
# For a multi-class issuer, company-facts drops the dimensional per-class share facts and
# what survives is the pre-2011 cover-page figure from before dimensional tagging was
# adopted. The concept is then PRESENT and parses cleanly, so nothing upstream refuses it:
# V resolved 469,280,842 measured 2010-01-27 and BRK.B a Class-A-scale 941,481 measured
# 2011-04-29, against fresh current-period revenue. V's market cap came out at $173.0B
# against a real ~$712B and it ranked first at half the target weight; BRK.B's at $836.8M
# against a real ~$1.1T, held back from the same outcome only by a missing numerator.
#
# Deliberately generous: a compliant filer restates this figure on every 10-Q cover page,
# so two years cannot reject anyone legitimate, while the failures it must catch are off by
# fifteen. A tighter, better-calibrated bound belongs with the vintage-carrying read path
# (#530) — this one only has to make an order-of-magnitude error impossible to publish.
_MAX_SHARES_STALENESS_DAYS = 730


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
    # The share count's own measurement date, carried for the same reason and additionally
    # enforced: `shares_outstanding` is None whenever this is staler than
    # `_MAX_SHARES_STALENESS_DAYS`, so a market capitalisation can never be built from a
    # figure the warehouse cannot date.
    shares_period_end: date | None = None
    # True when `gross_profit` is the revenue proxy rather than a reported or derived
    # figure. Reported rather than inferred, because "gross_profit happens to equal
    # revenue" is a coincidence for some issuers and the substitution must be auditable
    # (#533). The adapter refuses it for industries the proxy was not approved for.
    gross_profit_is_revenue_proxy: bool = False
    # The latest annual net income, which module 1's multiple divides the market cap by.
    net_income: Decimal | None = None
    # Every annual net-income period knowable at the cutoff, period end -> value.
    #
    # This adapter used to reduce that series to a single compound rate and ship the
    # scalar, because `staging.strategy_backtest_inputs` was keyed (issuer, cutoff,
    # input_key) with no period axis and a series simply could not cross. The reduction is
    # factor arithmetic, so it was computation in L0 -- exactly what init.md rule 2 and the
    # AGENTS.md red line forbid, and it was only there because the transport was too narrow
    # to express the alternative. The period axis (0043) removes that excuse: the series
    # crosses whole and `factors.base.peg` reduces it.
    #
    # `annual_values_by_period_end` has already applied the point-in-time filter and the
    # `_ANNUAL_MINIMUM_DAYS` duration floor, so what travels is annual periods that were
    # knowable at the cutoff -- the factor never re-selects a vintage (init.md rule 3).
    net_income_by_period: Mapping[date, Decimal] = field(default_factory=dict)


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
    # #496: the issuer's most recent successfully-parsed CIK from OUR OWN
    # capture lineage (raw.capture_source_vintages), resolved at planning.
    # Used ONLY when the index-mapped CIK's payload yields no eligible fact at
    # all (a real state: XOM's post-reorganization holdco CIK publishes no
    # us-gaap taxonomy). Registry/lineage-driven — never a ticker allowlist.
    predecessor_cik: int | None = None
    # #533: whether this issuer's industry may substitute revenue for an untagged gross
    # profit. Resolved from the EDGAR SIC at planning, exactly like `operating_branch`.
    # Defaults False so a target assembled without the registry cannot inherit a
    # substitution that only holds for ~zero-COGS industries.
    revenue_proxy_allowed: bool = False


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


def resolve_field(
    facts: dict[str, Any], ruleset: ConceptMappingRuleset, field: str, cutoff: date
) -> dict[date, _Datum]:
    """The annual series for one declared field, resolved by its DECLARED kind.

    Synonyms merge into one period-keyed series so the latest period wins across a tag
    switch; stand-ins stop at the first concept the issuer reports, so a different quantity
    can never be promoted by carrying a later date. Which one applies is data, not a
    decision this function makes.
    """
    mapping = ruleset.mapping_for(field)
    if mapping is None:
        return {}
    concepts = tuple((item.taxonomy, item.concept) for item in mapping.concepts)
    if mapping.kind is ResolutionKind.FALLBACK:
        return _preferred_variant(facts, concepts, mapping.unit, cutoff)
    return _merge_variants(facts, concepts, mapping.unit, cutoff)


def gross_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Reported `GrossProfit` or revenue minus cost of revenue — whichever covers the later period.

    "Reported wins unconditionally" is not safe: Amazon last tagged `GrossProfit` for
    FY2009 and mart carried that figure against FY2025 headcount, which is what made its
    reported GPPE negative (#496). A directly reported figure is still preferred when the
    two cover the *same* period — it is the issuer's own assertion rather than our
    arithmetic — but it cannot outrank a more recent one.
    """
    return _gross_profit_resolved(facts, cutoff, ruleset)[0]


def _gross_profit_resolved(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset
) -> tuple[_Datum | None, bool]:
    """Gross profit, and whether it is the revenue proxy rather than a real figure.

    The caller needs to know which, because the proxy is only valid for industries whose
    cost of revenue really is ~zero (#533) — and that is a property of the issuer, which
    this function cannot see.
    """
    direct = _latest(resolve_field(facts, ruleset, "gross_profit", cutoff))
    derived = _difference_at_shared_period(
        resolve_field(facts, ruleset, "revenue", cutoff),
        resolve_field(facts, ruleset, "cost_of_revenue", cutoff),
    )
    candidates = [datum for datum in (direct, derived) if datum is not None]
    if candidates:
        return max(candidates, key=lambda datum: datum.period_end), False
    if _files_no_cogs_concepts(facts, ruleset):
        # #496 owner decision (2026-07-28): an issuer whose company-facts carry NO
        # GrossProfit and NO COGS-family concept AT ALL uses revenue as the gross-profit
        # proxy — "payment networks carry ~zero COGS; the bias is small and its direction
        # known". Concept-level absence only: a mere period mismatch on a real COGS filer
        # still resolves to None.
        #
        # Absence of a tag is NOT evidence of absent cost, so the substitution is offered
        # here and accepted only for an industry the registry says qualifies. Read as
        # "~zero or structurally unreported" it also fired for XOM (Petroleum Refining),
        # publishing $332B of revenue as gross profit — $5.36M per employee, above NVIDIA
        # (#533).
        return _latest(resolve_field(facts, ruleset, "revenue", cutoff)), True
    return None, False


def _declared_concepts(ruleset: ConceptMappingRuleset, field: str) -> tuple[ConceptRef, ...]:
    mapping = ruleset.mapping_for(field)
    return () if mapping is None else mapping.concepts


def _files_no_cogs_concepts(facts: dict[str, Any], ruleset: ConceptMappingRuleset) -> bool:
    """True when the issuer tags NO gross-profit and NO cost concept the ruleset knows.

    Concept-level absence, read off the ruleset rather than a second copy of the list —
    two lists that must agree are two lists that eventually will not.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    declared = [
        item.concept
        for field in ("gross_profit", "cost_of_revenue")
        for item in _declared_concepts(ruleset, field)
        if item.taxonomy == "us-gaap"
    ]
    return all(concept not in gaap for concept in declared)


def insurance_pre_claims_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
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
    revenue = resolve_field(facts, ruleset, "revenue", cutoff)
    claims = resolve_field(facts, ruleset, "insurance_claims", cutoff)
    datum = _difference_at_shared_period(revenue, claims)
    if datum is None or datum.period_end != max(revenue):
        return None
    return datum


def pre_provision_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Bank pre-provision net revenue: net revenue minus noninterest expense (#59).

    The revenue side is a FALLBACK list, so it resolves by preference rather than by
    recency: `Revenues` for a bank is gross of interest expense, and subtracting
    noninterest expense from it yields something that is not pre-provision net revenue at
    all. It is reached only when the issuer publishes no net-of-interest total — never
    because it carries a later period end.
    """
    return _difference_at_shared_period(
        resolve_field(facts, ruleset, "bank_revenue", cutoff),
        resolve_field(facts, ruleset, "noninterest_expense", cutoff),
    )


def build_bundle(
    facts: dict[str, Any],
    cutoff: date,
    branch: OperatingBranch,
    *,
    raw_bytes: bytes | None = None,
    ruleset: ConceptMappingRuleset = DEFAULT_RULESET,
) -> FinancialFactsBundle:
    """Extract the PIT financial-fact bundle from a company-facts payload.

    A payload with no eligible fact is not a failure: the source honestly asserts
    nothing for this issuer at this cutoff, so every field resolves to `None` and the
    factor surfaces the gap (`missing_gross_profit`, …) instead of the capture
    inventing one. A source that could not be *reached* raises instead — that is a
    retryable failure, never a null-filled fact.
    """
    assets = _latest(resolve_field(facts, ruleset, "total_assets", cutoff))
    # The last-resort share count is consulted only when the point-in-time series is
    # EMPTY. Kept as a separate lookup rather than a trailing synonym so a period average
    # can never win on recency over a real point-in-time figure (#496).
    # A share count too old to describe today's company is refused, and refusing the
    # point-in-time series lets the period-average last resort answer instead of nothing —
    # which is the difference between MA (whose 2010 cover-page fact is the only `dei` one,
    # but whose weighted-average series is current) and V (where both are stale or absent).
    # Staleness is applied per candidate rather than to the winner, because `primary or
    # fallback` would otherwise let a sixteen-year-old primary block a usable fallback.
    shares_primary = _fresh_shares(_latest(resolve_field(facts, ruleset, "shares_outstanding", cutoff)), cutoff)
    shares_fallback = _fresh_shares(
        _latest(resolve_field(facts, ruleset, "shares_outstanding_last_resort", cutoff)), cutoff
    )
    shares = shares_primary or shares_fallback
    # When every candidate is refused the newest measurement date is still recorded, so the
    # warehouse says HOW stale the best candidate was rather than leaving an
    # indistinguishable null. Fail-closed is deliberate: the factor then reports
    # `missing_shares_outstanding` exactly as for an issuer that never filed one, and an
    # honest gap outranks a market capitalisation wrong by three orders of magnitude.
    shares_period_end = _newest_period_end(
        shares,
        _latest(resolve_field(facts, ruleset, "shares_outstanding", cutoff)),
        _latest(resolve_field(facts, ruleset, "shares_outstanding_last_resort", cutoff)),
    )
    revenue = _latest(resolve_field(facts, ruleset, "revenue", cutoff))
    # The growth basis for module 1. `resolve_field` already returns EVERY annual period
    # knowable at the cutoff, filed-date-resolved and duration-filtered, so the whole
    # series is here and the bundle simply stops discarding it.
    earnings_periods = resolve_field(facts, ruleset, "net_income", cutoff)
    net_income = _latest(earnings_periods) if earnings_periods else None
    # large_model_value_v0 applies one uniform capital-adjusted formula to every issuer;
    # the branch only decides WHICH versioned extraction asserts the operating numerator:
    # banks use the pre-provision proxy, insurers revenue-minus-claims (#496), everyone
    # else reported/derived gross profit (with the no-COGS revenue proxy inside).
    is_revenue_proxy = False
    if branch is OperatingBranch.FINANCIAL:
        profit = pre_provision_profit(facts, cutoff, ruleset)
    elif branch is OperatingBranch.INSURANCE:
        profit = insurance_pre_claims_profit(facts, cutoff, ruleset)
    else:
        profit, is_revenue_proxy = _gross_profit_resolved(facts, cutoff, ruleset)
    # EVERY earnings period the series carries, not just the window's two ends. The rate
    # downstream rests on all of them, so the payload is knowable only once the latest of
    # them was filed — the PIT obligation #284 named and could not satisfy while only the
    # endpoints travelled.
    resolved = [datum for datum in (profit, assets, shares, revenue, *earnings_periods.values()) if datum is not None]
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
        shares_period_end=shares_period_end,
        gross_profit_is_revenue_proxy=is_revenue_proxy,
        net_income=_v(net_income),
        net_income_by_period={end: datum.value for end, datum in sorted(earnings_periods.items())},
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
        mapping_version: str = MAPPING_VERSION,
    ) -> None:
        self._targets = targets
        self._fetcher = fetcher
        self._headcount_extractor = headcount_extractor
        # Carries the resolved ruleset's hash, so an observation names the exact concept
        # rules behind it. Without that, advancing the pointer silently changes what
        # numbers mean while every row still claims the same mapping identity.
        self._mapping_version = mapping_version

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        target = self._targets.get(work_item.work_item_id)
        if target is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        try:
            bundle = self._fetcher(target.cik, target.cutoff, target.operating_branch)
            if bundle is not None and bundle.knowable_at is None and target.predecessor_cik is not None:
                # #496 predecessor-CIK fallback: the mapped CIK's document
                # exists but asserts nothing (empty taxonomy after a corporate
                # reorganization); the issuer's own capture lineage names the
                # CIK that last parsed successfully. Both payloads end up
                # archived (the empty one deduped from prior runs).
                bundle = self._fetcher(target.predecessor_cik, target.cutoff, target.operating_branch)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)
        if bundle is None:
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if bundle.knowable_at is not None and bundle.knowable_at.date() > target.cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        # #533: the revenue-for-gross-profit substitution is valid only where cost of
        # revenue really is ~zero, which is a property of the issuer's industry and so is
        # only knowable here, on the target, rather than inside the parse. Refusing it
        # leaves the honest gap the issuer had before the substitution existed: the factor
        # reports `missing_gross_profit` and the issuer is excluded, which is strictly
        # better than ranking an oil major above NVIDIA on labour efficiency.
        gross_profit_value = bundle.gross_profit
        if bundle.gross_profit_is_revenue_proxy and not target.revenue_proxy_allowed:
            gross_profit_value = None
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
            "gross_profit": _s(gross_profit_value),
            "total_assets": _s(bundle.total_assets),
            "headcount": _s(headcount),
            "revenue": _s(bundle.revenue),
            "shares_outstanding": _s(bundle.shares_outstanding),
            "pre_provision_profit": _s(bundle.pre_provision_profit),
            "operating_period_end": _d(bundle.operating_period_end),
            "revenue_period_end": _d(bundle.revenue_period_end),
            "shares_period_end": _d(bundle.shares_period_end),
            "net_income": _s(bundle.net_income),
            # Sorted so the payload hash depends on the series, not on dict insertion order.
            "net_income_by_period": {
                end.isoformat(): _s(value) for end, value in sorted(bundle.net_income_by_period.items())
            },
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
            record=NormalizedRecord(
                payload=payload, parser_version=PARSER_VERSION, mapping_version=self._mapping_version
            ),
        )


def _confidence(payload: Mapping[str, Any]) -> Decimal:
    """Per-source-class confidence prior (#207/#404); the calibrated formula is #337."""
    present = sum(payload.get(field) is not None for field in ("gross_profit", "total_assets", "shares_outstanding"))
    return {3: Decimal("0.92"), 2: Decimal("0.80"), 1: Decimal("0.65")}.get(present, Decimal("0.50"))


def _fresh_shares(datum: _Datum | None, cutoff: date) -> _Datum | None:
    """The datum, or None when it was measured too long before the cutoff (#529)."""
    if datum is None or (cutoff - datum.period_end).days > _MAX_SHARES_STALENESS_DAYS:
        return None
    return datum


def _newest_period_end(*candidates: _Datum | None) -> date | None:
    """The latest measurement date among the candidates, refused ones included."""
    ends = [datum.period_end for datum in candidates if datum is not None]
    return max(ends) if ends else None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def sec_financial_fetcher(
    cik: int, cutoff: date, branch: OperatingBranch, *, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> FinancialFactsBundle:
    """Default fetcher: the real SEC company-facts client, parsed point-in-time.

    Imported lazily so the adapter and its tests carry no network dependency.
    """

    import httpx

    from data_engine.sources import sec

    try:
        body, facts = sec.fetch_company_facts_response(cik)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    return build_bundle(facts, cutoff, branch, raw_bytes=body, ruleset=ruleset)
