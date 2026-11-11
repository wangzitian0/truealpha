"""SEC company-facts financial-fact source adapter (ADR A1 / #171)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import psycopg
from factors.production_topt import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset, ConceptRef, ResolutionKind
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode
from truealpha_contracts.strategy import load_live_strategy_definition

from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.production_topt.corroboration_audit import FETCH, record_lost_corroboration
from data_engine.datahub.production_topt.executor import (
    Corroboration,
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.issuer_registry import (
    ANNUAL_STATEMENT_FORMS,
    INTERIM_STATEMENT_FORMS,
    STATEMENT_FORMS,
    UNAVAILABLE_PAYLOAD_KEY,
    StatementFiling,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION
from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant
from data_engine.datahub.share_basis import MISSING_CLASS_RATIO
from data_engine.sources.gateway import BudgetExhausted, CapacityExceeded

if TYPE_CHECKING:
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
    from data_engine.datahub.standards.listing_unit_ratio import ListingUnitRatio

_ANNUAL_MINIMUM_DAYS = 350
_DOCUMENT_KEY = "_document"
_INSTANT_INPUTS = frozenset({"total_assets", "shares_outstanding", "financial_assets"})
_ANNUAL_INPUTS = frozenset({"gross_profit", "revenue", "net_income", "financial_returns"})
SHARES_POINT_IN_TIME = "point_in_time"
SHARES_WEIGHTED_AVERAGE_BASIC = "weighted_average_basic"

CORROBORATED_FINANCIAL_FIELDS: tuple[str, ...] = (
    "revenue",
    "net_income",
    "gross_profit",
    "total_assets",
    "eps_basic",
    "eps_diluted",
)
_EVIDENCE_TOKENS = ("accession", "form", "filed", "raw")
_DOMESTIC_10K_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
_FOREIGN_ANNUAL_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})

_INVESTMENT_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("short_term_investments+long_term_investments", ("short_term_investments", "long_term_investments")),
    ("marketable_securities", ("marketable_securities_current", "marketable_securities_noncurrent")),
    ("securities", ("afs_debt_securities", "htm_securities", "equity_securities_fvni")),
    ("investments_total", ("investments_total",)),
)
_BANK_PORTFOLIO_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "securities+fed_funds_sold",
        ("afs_debt_securities", "htm_securities", "equity_securities_fvni", "fed_funds_sold"),
    ),
    ("investments_total", ("investments_total",)),
)


@dataclass(frozen=True)
class FinancialFactsBundle:
    """Point-in-time annual figures yielded by one company-facts payload at a cutoff."""

    gross_profit: Decimal | None
    total_assets: Decimal | None
    shares_outstanding: Decimal | None
    revenue: Decimal | None
    pre_provision_profit: Decimal | None
    raw_bytes: bytes
    knowable_at: datetime | None
    operating_period_end: date | None = None
    revenue_period_end: date | None = None
    shares_period_end: date | None = None
    period_start: date | None = None
    net_income: Decimal | None = None
    net_income_by_period: Mapping[date, Decimal] = field(default_factory=dict)
    vintages: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    financial_assets: Decimal | None = None
    financial_assets_basis: str | None = None
    financial_returns: Decimal | None = None
    financial_returns_basis: str | None = None
    financial_returns_is_proxy: bool = False
    shares_basis: str | None = None
    latest_statement_period_end: date | None = None
    latest_annual_period_end: date | None = None
    document: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    stockholders_equity: Decimal | None = None
    preferred_stock_value: Decimal | None = None
    goodwill: Decimal | None = None
    intangible_assets_net_excluding_goodwill: Decimal | None = None
    intangible_basis: str | None = None
    tangible_equity_period_end: date | None = None
    bank_net_revenue: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class FinancialComponents:
    """Financial assets and returns resolved by declared preference."""

    assets: _Datum | None
    assets_basis: str | None
    returns: _Datum | None
    returns_basis: str | None
    returns_is_proxy: bool


@dataclass(frozen=True)
class SecTarget:
    """Resolved fetch coordinates for one financial-fact work item."""

    cik: int
    cutoff: date
    issuer_id: str
    instrument_id: str
    listing_id: str
    operating_branch: OperatingBranch
    predecessor_cik: int | None = None
    predecessor_signed: bool = False
    statement_filings: tuple[StatementFiling, ...] = ()
    registry_unavailable: bool = False
    ticker: str = ""


FinancialFactsFetcher = Callable[[int, date, OperatingBranch], FinancialFactsBundle | None]


@dataclass(frozen=True)
class FinancialFactAssertion:
    """Annual figures asserted by a corroborating second origin at a cutoff."""

    raw_bytes: bytes
    period_end: date
    values: Mapping[str, Decimal | None]
    by_period_end: Mapping[date, Mapping[str, Decimal | None]]
    knowable_at: datetime
    currency: str | None = None
    accounting_standards: str | None = None


FinancialFactCorroborator = Callable[[str, date], FinancialFactAssertion | None]


@dataclass(frozen=True)
class FinancialFactCorroboratingOrigin:
    """Independent second financial-fact origin with its parser identity."""

    origin: str
    parser_version: str
    mapping_version: str
    fetch: FinancialFactCorroborator
    confidence: Decimal | None = None
    raw_source: DataSource = DataSource.MOOMOO


@dataclass(frozen=True)
class HeadcountFact:
    """Point-in-time employee headcount extracted from filing text (#70)."""

    value: Decimal
    knowable_at: datetime
    source: str | None = None
    evidence_ref: str | None = None
    period_end: date | None = None
    confidence: Decimal | None = None


def headcount_vintage(fact: HeadcountFact) -> dict[str, Any]:
    """Serialize headcount evidence tokens (`accession`, `form`, `filed`, `raw`) as JSON-ready metadata."""
    parsed: dict[str, str | None] = dict.fromkeys(_EVIDENCE_TOKENS)
    for token in (fact.evidence_ref or "").split():
        key, separator, value = token.partition("=")
        if separator and key in parsed and parsed[key] is None:
            parsed[key] = value or None
    return {
        "accession": parsed["accession"],
        "confidence": None if fact.confidence is None else str(fact.confidence),
        "evidence_ref": fact.evidence_ref,
        "filed": parsed["filed"],
        "form": parsed["form"],
        "knowable_at": fact.knowable_at.isoformat(),
        "period_end": None if fact.period_end is None else fact.period_end.isoformat(),
        "raw": parsed["raw"],
        "source": fact.source,
    }


HeadcountExtractor = Callable[[int, date], HeadcountFact | None]
ListingRatioReader = Callable[[int, date, str], "ListingUnitRatio"]


class SourceUnavailableError(Exception):
    """Transient SEC failure that the executor retries."""


@dataclass(frozen=True)
class _Datum:
    """One eligible XBRL fact with its filing metadata and fiscal period."""

    value: Decimal
    filed: date
    period_end: date
    period_start: date | None = None
    accession: str | None = None
    form: str | None = None
    fy: int | None = None
    fp: str | None = None
    statement_form: bool | None = None
    document: str | None = None

    @property
    def rank(self) -> int:
        return 1 if self.statement_form is False else 0


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _statement_form(form: str | None) -> bool | None:
    return None if form is None else form in STATEMENT_FORMS


def _combined_statement_form(datums: Sequence[_Datum]) -> bool | None:
    flags = [datum.statement_form for datum in datums]
    if False in flags:
        return False
    return None if None in flags else True


def _combined_document(datums: Sequence[_Datum]) -> str | None:
    documents = sorted({datum.document for datum in datums if datum.document is not None})
    return "+".join(documents) if documents else None


def _segment_key(entry: Mapping[str, Any], unit: str) -> str | None:
    if unit != "shares":
        return None
    segment = entry.get("segment")
    if segment is None:
        return None
    if isinstance(segment, Mapping):
        return json.dumps(segment, sort_keys=True, separators=(",", ":"))
    return _text_or_none(segment)


def _record_better_datum(values: dict[date, _Datum], end: date, datum: _Datum) -> None:
    existing = values.get(end)
    if existing is None or (datum.rank, -datum.filed.toordinal()) < (existing.rank, -existing.filed.toordinal()):
        values[end] = datum


def _parse_entry_datum(entry: Mapping[str, Any], *, cutoff: date, allow_duration: bool) -> _Datum | None:
    filed_raw, end_raw, start_raw, val = entry.get("filed"), entry.get("end"), entry.get("start"), entry.get("val")
    if filed_raw is None or end_raw is None or val is None:
        return None
    if not allow_duration and start_raw is not None:
        return None
    form = _text_or_none(entry.get("form"))
    try:
        filed, end = date.fromisoformat(filed_raw), date.fromisoformat(end_raw)
        start = date.fromisoformat(start_raw) if start_raw is not None else None
        if start is not None and (form in INTERIM_STATEMENT_FORMS or (end - start).days < _ANNUAL_MINIMUM_DAYS):
            return None
        value = Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None
    if filed > cutoff:
        return None
    fy_raw = entry.get("fy")
    return _Datum(
        value=value,
        filed=filed,
        period_end=end,
        period_start=start,
        accession=_text_or_none(entry.get("accn")),
        form=form,
        fy=int(fy_raw) if isinstance(fy_raw, int) or (isinstance(fy_raw, str) and fy_raw.isdigit()) else None,
        fp=_text_or_none(entry.get("fp")),
        statement_form=_statement_form(form),
        document=_text_or_none(entry.get(_DOCUMENT_KEY)),
    )


def annual_values_by_period_end(
    facts: dict[str, Any], taxonomy: str, concept: str, unit: str, cutoff: date
) -> dict[date, _Datum]:
    """Annual or instant values of one XBRL concept knowable at `cutoff`, keyed by period end."""
    entries = facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit)
    if not entries:
        return {}
    values: dict[date, _Datum] = {}
    for entry in entries:
        if _segment_key(entry, unit) is not None:
            continue
        datum = _parse_entry_datum(entry, cutoff=cutoff, allow_duration=True)
        if datum is not None:
            _record_better_datum(values, datum.period_end, datum)
    return values


def _latest(values: dict[date, _Datum]) -> _Datum | None:
    return values[max(values)] if values else None


def _best_candidate(*candidates: _Datum | None) -> _Datum | None:
    present = [datum for datum in candidates if datum is not None]
    return max(present, key=lambda datum: (datum.period_end, -datum.rank)) if present else None


def _merge_variants(
    facts: dict[str, Any], concepts: Sequence[tuple[str, str]], unit: str, cutoff: date
) -> dict[date, _Datum]:
    merged: dict[date, _Datum] = {}
    for taxonomy, concept in concepts:
        for end, datum in annual_values_by_period_end(facts, taxonomy, concept, unit, cutoff).items():
            existing = merged.get(end)
            if existing is None or datum.rank < existing.rank:
                merged[end] = datum
    return merged


def _preferred_variant(
    facts: dict[str, Any], concepts: Sequence[tuple[str, str]], unit: str, cutoff: date
) -> dict[date, _Datum]:
    series = [annual_values_by_period_end(facts, taxonomy, concept, unit, cutoff) for taxonomy, concept in concepts]
    for values in series:
        if any(datum.rank == 0 for datum in values.values()):
            return values
    return next((values for values in series if values), {})


def _difference_at_shared_period(base: dict[date, _Datum], subtracted: dict[date, _Datum]) -> _Datum | None:
    shared = set(base) & set(subtracted)
    if not shared:
        return None
    end = max(shared)
    later = base[end] if base[end].filed >= subtracted[end].filed else subtracted[end]
    return _Datum(
        value=base[end].value - subtracted[end].value,
        filed=later.filed,
        period_end=end,
        period_start=base[end].period_start or subtracted[end].period_start,
        accession=later.accession,
        form=later.form,
        fy=later.fy,
        fp=later.fp,
        statement_form=_combined_statement_form((base[end], subtracted[end])),
        document=_combined_document((base[end], subtracted[end])),
    )


def resolve_field(
    facts: dict[str, Any], ruleset: ConceptMappingRuleset, field_name: str, cutoff: date
) -> dict[date, _Datum]:
    """Resolve the annual series for one declared field by its `ResolutionKind`."""
    mapping = ruleset.mapping_for(field_name)
    if mapping is None:
        return {}
    concepts = tuple((item.taxonomy, item.concept) for item in mapping.concepts)
    if mapping.kind is ResolutionKind.FALLBACK:
        return _preferred_variant(facts, concepts, mapping.unit, cutoff)
    return _merge_variants(facts, concepts, mapping.unit, cutoff)


def _field_series_memo(
    facts: dict[str, Any], ruleset: ConceptMappingRuleset, cutoff: date
) -> Callable[[str], dict[date, _Datum]]:
    memo: dict[str, dict[date, _Datum]] = {}

    def series(field_name: str) -> dict[date, _Datum]:
        if field_name not in memo:
            memo[field_name] = resolve_field(facts, ruleset, field_name, cutoff)
        return memo[field_name]

    return series


def gross_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Reported `GrossProfit` or `revenue - cost_of_revenue`, whichever covers the later period."""
    direct = _latest(resolve_field(facts, ruleset, "gross_profit", cutoff))
    derived = _difference_at_shared_period(
        resolve_field(facts, ruleset, "revenue", cutoff),
        resolve_field(facts, ruleset, "cost_of_revenue", cutoff),
    )
    return _best_candidate(direct, derived)


def _declared_concepts(ruleset: ConceptMappingRuleset, field_name: str) -> tuple[ConceptRef, ...]:
    mapping = ruleset.mapping_for(field_name)
    return () if mapping is None else mapping.concepts


def insurance_pre_claims_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Insurance operating numerator: revenue minus policyholder benefits/claims at the latest revenue period."""
    revenue = resolve_field(facts, ruleset, "revenue", cutoff)
    claims = resolve_field(facts, ruleset, "insurance_claims", cutoff)
    datum = _difference_at_shared_period(revenue, claims)
    return None if datum is None or datum.period_end != max(revenue) else datum


def pre_provision_profit(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Bank pre-provision net revenue: net revenue minus noninterest expense."""
    return _difference_at_shared_period(
        resolve_field(facts, ruleset, "bank_revenue", cutoff),
        resolve_field(facts, ruleset, "noninterest_expense", cutoff),
    )


def bank_net_revenue(
    facts: dict[str, Any], cutoff: date, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> _Datum | None:
    """Bank net revenue (`RevenuesNetOfInterestExpense` or `InterestIncomeExpenseNet + NoninterestIncome`)."""
    direct = _latest(resolve_field(facts, ruleset, "bank_net_revenue", cutoff))
    nii = resolve_field(facts, ruleset, "interest_income_expense_net", cutoff)
    nonii = resolve_field(facts, ruleset, "noninterest_income", cutoff)
    shared = sorted(set(nii) & set(nonii))
    derived = _sum_at(shared[-1], [nii[shared[-1]], nonii[shared[-1]]]) if shared else None
    return _best_candidate(direct, derived) or _latest(resolve_field(facts, ruleset, "bank_revenue", cutoff))


def _sum_at(end: date, datums: Sequence[_Datum]) -> _Datum:
    later = max(datums, key=lambda datum: datum.filed)
    return _Datum(
        value=sum((datum.value for datum in datums), Decimal(0)),
        filed=later.filed,
        period_end=end,
        accession=later.accession,
        form=later.form,
        fy=later.fy,
        fp=later.fp,
        statement_form=_combined_statement_form(datums),
        document=_combined_document(datums),
    )


def _tier_at(
    series: Callable[[str], dict[date, _Datum]], end: date, fields: Sequence[str]
) -> tuple[list[_Datum], list[str]]:
    datums: list[_Datum] = []
    named: list[str] = []
    for field_name in fields:
        resolved = series(field_name)
        if end in resolved:
            datums.append(resolved[end])
            named.append(field_name)
    return datums, named


def financial_components(
    facts: dict[str, Any], cutoff: date, branch: OperatingBranch, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> FinancialComponents:
    """Resolve `financial_assets` and `financial_returns` by declared tier preference (#528)."""
    series = _field_series_memo(facts, ruleset, cutoff)
    assets: _Datum | None = None
    assets_basis: str | None = None
    if branch is OperatingBranch.FINANCIAL:
        for _basis, fields in _BANK_PORTFOLIO_TIERS:
            ends = {end for field_name in fields for end in series(field_name)}
            if ends:
                end = max(ends)
                datums, named = _tier_at(series, end, fields)
                assets, assets_basis = _sum_at(end, datums), "+".join(named)
                break
        return FinancialComponents(assets, assets_basis, None, None, False)

    cash = _latest(series("cash_and_equivalents"))
    combined = _latest(series("cash_and_short_term_investments"))
    if cash is not None:
        end = cash.period_end
        for _basis, fields in _INVESTMENT_TIERS:
            datums, named = _tier_at(series, end, fields)
            if datums:
                assets, assets_basis = _sum_at(end, [cash, *datums]), "cash_and_equivalents+" + "+".join(named)
                break
        else:
            assets, assets_basis = cash, "cash_and_equivalents"
    elif combined is not None:
        end = combined.period_end
        datums, named = _tier_at(series, end, ("long_term_investments",))
        assets = _sum_at(end, [combined, *datums]) if datums else combined
        assets_basis = "cash_and_short_term_investments" + ("+" + "+".join(named) if named else "")

    returns: _Datum | None = None
    returns_basis: str | None = None
    proxy = False
    income = _latest(series("investment_income"))
    if income is not None:
        parts, named = [income], ["investment_income"]
        for field_name in ("investment_income_dividend", "investment_gains"):
            extra = series(field_name).get(income.period_end)
            if extra is not None:
                parts.append(extra)
                named.append(field_name)
        returns, returns_basis = _sum_at(income.period_end, parts), "+".join(named)
    else:
        nonoperating = _latest(series("nonoperating_income"))
        if nonoperating is not None:
            returns, returns_basis, proxy = nonoperating, "nonoperating_income", True
    return FinancialComponents(assets, assets_basis, returns, returns_basis, proxy)


def _segmented_shares_datum(facts: dict[str, Any], ruleset: ConceptMappingRuleset, cutoff: date) -> _Datum | None:
    """Newest fresh per-class segmented share entry on `shares_outstanding` (#1519)."""
    mapping = ruleset.mapping_for("shares_outstanding")
    if mapping is None:
        return None
    candidates: dict[date, _Datum] = {}
    for item in mapping.concepts:
        entries = facts.get("facts", {}).get(item.taxonomy, {}).get(item.concept, {}).get("units", {}).get(mapping.unit)
        for entry in entries or ():
            if _segment_key(entry, mapping.unit) is None:
                continue
            datum = _parse_entry_datum(entry, cutoff=cutoff, allow_duration=False)
            if datum is not None:
                _record_better_datum(candidates, datum.period_end, datum)
    return _fresh_shares(_latest(candidates), cutoff)


def _shares_basis(
    shares_primary: _Datum | None,
    shares_fallback: _Datum | None,
    segmented_shares: _Datum | None = None,
) -> str | None:
    if shares_primary is not None:
        return SHARES_POINT_IN_TIME
    if shares_fallback is not None:
        return SHARES_WEIGHTED_AVERAGE_BASIC
    if segmented_shares is not None:
        return MISSING_CLASS_RATIO
    return None


def _ruleset_currency(ruleset: ConceptMappingRuleset) -> str | None:
    mapping = ruleset.mapping_for("revenue") or ruleset.mapping_for("total_assets")
    return None if mapping is None else mapping.unit


def build_bundle(
    facts: dict[str, Any],
    cutoff: date,
    branch: OperatingBranch,
    *,
    raw_bytes: bytes | None = None,
    ruleset: ConceptMappingRuleset = DEFAULT_RULESET,
) -> FinancialFactsBundle:
    """Extract the PIT financial-fact bundle from a company-facts payload."""
    assets = _latest(resolve_field(facts, ruleset, "total_assets", cutoff))
    raw_shares_primary = _latest(resolve_field(facts, ruleset, "shares_outstanding", cutoff))
    raw_shares_fallback = _latest(resolve_field(facts, ruleset, "shares_outstanding_last_resort", cutoff))
    shares_primary = _fresh_shares(raw_shares_primary, cutoff)
    shares_fallback = _fresh_shares(raw_shares_fallback, cutoff)
    segmented_shares = (
        _segmented_shares_datum(facts, ruleset, cutoff) if shares_primary is None and shares_fallback is None else None
    )
    shares = shares_primary or shares_fallback
    shares_basis = _shares_basis(shares_primary, shares_fallback, segmented_shares)
    shares_period_end = _newest_period_end(shares, segmented_shares, raw_shares_primary, raw_shares_fallback)

    revenue = _latest(resolve_field(facts, ruleset, "revenue", cutoff))
    earnings_periods = resolve_field(facts, ruleset, "net_income", cutoff)
    net_income = _latest(earnings_periods) if earnings_periods else None

    pre_provision: _Datum | None = None
    bank_net_rev: _Datum | None = None
    insurer_profit: _Datum | None = None
    gp_datum: _Datum | None = None
    if branch is OperatingBranch.FINANCIAL:
        pre_provision = pre_provision_profit(facts, cutoff, ruleset)
        bank_net_rev = bank_net_revenue(facts, cutoff, ruleset)
        profit = pre_provision
    elif branch is OperatingBranch.INSURANCE:
        insurer_profit = insurance_pre_claims_profit(facts, cutoff, ruleset)
        profit = insurer_profit
        gp_datum = insurer_profit
    else:
        gp_datum = gross_profit(facts, cutoff, ruleset)
        profit = gp_datum

    financial = financial_components(facts, cutoff, branch, ruleset)
    tangible = tangible_common_equity_components(facts, cutoff, branch, ruleset)
    resolved = [
        datum
        for datum in (
            profit,
            bank_net_rev,
            assets,
            shares,
            revenue,
            financial.assets,
            financial.returns,
            tangible.stockholders_equity,
            tangible.preferred_stock_value,
            tangible.goodwill,
            tangible.intangible_assets,
            *earnings_periods.values(),
        )
        if datum is not None
    ]
    knowable = max((datum.filed for datum in resolved), default=None)
    vintages: dict[str, Any] = {
        name: _vintage(datum)
        for name, datum in (
            ("gross_profit", gp_datum),
            ("pre_provision_profit", pre_provision),
            ("bank_net_revenue", bank_net_rev),
            ("insurance_pre_claims_profit", insurer_profit),
            ("total_assets", assets),
            ("shares_outstanding", shares),
            ("shares_weighted_average_basic", shares_fallback if shares_primary is None else None),
            ("revenue", revenue),
            ("net_income", net_income),
            ("financial_assets", financial.assets),
            ("financial_returns", financial.returns),
            ("stockholders_equity", tangible.stockholders_equity),
            ("preferred_stock_value", tangible.preferred_stock_value),
            ("goodwill", tangible.goodwill),
            ("intangible_assets_net_excluding_goodwill", tangible.intangible_assets),
        )
        if datum is not None
    }
    if earnings_periods:
        vintages["net_income_periods"] = {
            end.isoformat(): _vintage(datum) for end, datum in sorted(earnings_periods.items())
        }
    latest_statement, latest_annual = _statement_period_ends(facts, ruleset, cutoff)
    period_start = (profit.period_start if profit is not None else None) or (
        revenue.period_start if revenue is not None else None
    )
    return FinancialFactsBundle(
        gross_profit=_v(gp_datum),
        total_assets=_v(assets),
        shares_outstanding=_v(shares),
        revenue=_v(revenue),
        pre_provision_profit=_v(pre_provision),
        raw_bytes=(
            raw_bytes if raw_bytes is not None else json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        ),
        knowable_at=None if knowable is None else knowable_instant(AsOfAxis.ACCESSION, knowable),
        operating_period_end=None if profit is None else profit.period_end,
        revenue_period_end=None if revenue is None else revenue.period_end,
        shares_period_end=shares_period_end,
        period_start=period_start,
        net_income=_v(net_income),
        net_income_by_period={end: datum.value for end, datum in sorted(earnings_periods.items())},
        vintages=vintages,
        financial_assets=_v(financial.assets),
        financial_assets_basis=financial.assets_basis,
        financial_returns=_v(financial.returns),
        financial_returns_basis=financial.returns_basis,
        financial_returns_is_proxy=financial.returns_is_proxy,
        shares_basis=shares_basis,
        latest_statement_period_end=latest_statement,
        latest_annual_period_end=latest_annual,
        document=facts,
        stockholders_equity=_v(tangible.stockholders_equity),
        preferred_stock_value=_v(tangible.preferred_stock_value),
        goodwill=_v(tangible.goodwill),
        intangible_assets_net_excluding_goodwill=_v(tangible.intangible_assets),
        intangible_basis=tangible.intangible_basis,
        tangible_equity_period_end=tangible.period_end,
        bank_net_revenue=_v(bank_net_rev),
        currency=_ruleset_currency(ruleset),
    )


def _statement_period_ends(
    facts: Mapping[str, Any], ruleset: ConceptMappingRuleset, cutoff: date
) -> tuple[date | None, date | None]:
    """Newest balance-sheet date filed in any periodic statement and in an annual statement."""
    latest: date | None = None
    latest_annual: date | None = None
    for item in _declared_concepts(ruleset, "total_assets"):
        body = facts.get("facts", {}).get(item.taxonomy, {}).get(item.concept, {})
        for entries in body.get("units", {}).values():
            for entry in entries:
                form, filed_raw, end_raw = entry.get("form"), entry.get("filed"), entry.get("end")
                if form not in STATEMENT_FORMS or entry.get("start") is not None or not filed_raw or not end_raw:
                    continue
                try:
                    filed, end = date.fromisoformat(filed_raw), date.fromisoformat(end_raw)
                except ValueError:
                    continue
                if filed > cutoff:
                    continue
                latest = end if latest is None else max(latest, end)
                if form in ANNUAL_STATEMENT_FORMS:
                    latest_annual = end if latest_annual is None else max(latest_annual, end)
    return latest, latest_annual


def company_facts_record_id(cik: int) -> str:
    return f"companyfacts:CIK{cik:010d}"


def merge_company_facts(
    documents: Sequence[tuple[str, Mapping[str, Any]]], ruleset: ConceptMappingRuleset
) -> dict[str, Any]:
    """Pool declared concepts across an issuer's company-facts documents under multiple CIKs."""
    declared = {(item.taxonomy, item.concept) for mapping in ruleset.mappings for item in mapping.concepts}
    merged: dict[str, Any] = {"facts": {}}
    for record_id, document in documents:
        for taxonomy, concept in sorted(declared):
            body = document.get("facts", {}).get(taxonomy, {}).get(concept)
            if not body:
                continue
            units = merged["facts"].setdefault(taxonomy, {}).setdefault(concept, {"units": {}})["units"]
            for unit, entries in body.get("units", {}).items():
                units.setdefault(unit, []).extend({**entry, _DOCUMENT_KEY: record_id} for entry in entries)
    return merged


@dataclass(frozen=True)
class TangibleCommonEquityComponents:
    """Balance-sheet facts required for banking tangible common equity (#1176)."""

    stockholders_equity: _Datum | None = None
    preferred_stock_value: _Datum | None = None
    goodwill: _Datum | None = None
    intangible_assets: _Datum | None = None
    intangible_basis: str | None = None
    period_end: date | None = None


def _intangibles_at(series: Callable[[str], dict[date, _Datum]], end: date) -> tuple[_Datum | None, str | None]:
    total = series("intangible_assets_net_excluding_goodwill").get(end)
    if total is not None:
        return total, "IntangibleAssetsNetExcludingGoodwill"
    finite = series("finite_lived_intangibles_net").get(end)
    indefinite = series("indefinite_lived_intangibles").get(end)
    if finite is None or indefinite is None:
        return None, None
    return (
        _sum_at(end, [finite, indefinite]),
        "FiniteLivedIntangibleAssetsNet+IndefiniteLivedIntangibleAssetsExcludingGoodwill",
    )


def tangible_common_equity_components(
    facts: dict[str, Any], cutoff: date, branch: OperatingBranch, ruleset: ConceptMappingRuleset = DEFAULT_RULESET
) -> TangibleCommonEquityComponents:
    """Resolve banking tangible-common-equity components at the latest period carrying all three."""
    if branch is not OperatingBranch.FINANCIAL:
        return TangibleCommonEquityComponents()
    series = _field_series_memo(facts, ruleset, cutoff)
    equity = series("stockholders_equity")
    goodwill = series("goodwill")
    for end in sorted(set(equity) & set(goodwill), reverse=True):
        intangibles, basis = _intangibles_at(series, end)
        if intangibles is not None:
            return TangibleCommonEquityComponents(
                stockholders_equity=equity[end],
                preferred_stock_value=series("preferred_stock_value").get(end),
                goodwill=goodwill[end],
                intangible_assets=intangibles,
                intangible_basis=basis,
                period_end=end,
            )
    return TangibleCommonEquityComponents()


def _vintage(datum: _Datum) -> dict[str, Any]:
    return {
        "accession": datum.accession,
        "document": datum.document,
        "filed": datum.filed.isoformat(),
        "form": datum.form,
        "fp": datum.fp,
        "fy": datum.fy,
        "period_end": datum.period_end.isoformat(),
        "statement_form": datum.statement_form,
    }


def _v(datum: _Datum | None) -> Decimal | None:
    return None if datum is None else datum.value


def _asserts_income(bundle: FinancialFactsBundle) -> bool:
    return any(value is not None for value in (bundle.revenue, bundle.gross_profit, bundle.pre_provision_profit))


def _payload_vintage(
    bundle: FinancialFactsBundle,
    gross_profit_value: Decimal | None,
    headcount_evidence: dict[str, Any] | None,
    bank_net_revenue_value: Decimal | None = None,
) -> dict[str, Any]:
    vintage = {
        key: bundle.vintages[key]
        for key in sorted(bundle.vintages)
        if not (key == "gross_profit" and gross_profit_value is None)
        and not (key == "bank_net_revenue" and bank_net_revenue_value is None)
    }
    if headcount_evidence is not None:
        vintage["headcount"] = headcount_evidence
    return {key: vintage[key] for key in sorted(vintage)}


class SecFinancialFactAdapter:
    """`SourceFetchPort` for `financial-fact`, backed by an injected company-facts fetcher."""

    def __init__(
        self,
        targets: dict[str, SecTarget],
        fetcher: FinancialFactsFetcher,
        *,
        headcount_extractor: HeadcountExtractor | None = None,
        mapping_version: str = MAPPING_VERSION,
        corroborating_origins: Sequence[FinancialFactCorroboratingOrigin] = (),
        ruleset: ConceptMappingRuleset = DEFAULT_RULESET,
        listing_ratio_reader: ListingRatioReader | None = None,
    ) -> None:
        self._targets = targets
        self._fetcher = fetcher
        self._headcount_extractor = headcount_extractor
        self._listing_ratio_reader = listing_ratio_reader
        self._mapping_version = mapping_version
        self._corroborating_origins = tuple(corroborating_origins)
        self._ruleset = ruleset

    def _corroborate(self, target: SecTarget) -> tuple[Corroboration, ...]:
        if not target.ticker:
            return ()
        found: list[Corroboration] = []
        for origin in self._corroborating_origins:
            try:
                assertion = origin.fetch(target.ticker, target.cutoff)
            except Exception as error:  # noqa: BLE001
                record_lost_corroboration(origin.origin, FETCH, target.ticker, error)
                continue
            if assertion is None or assertion.knowable_at.date() > target.cutoff:
                continue
            payload: dict[str, Any] = {
                "issuer_id": target.issuer_id,
                "instrument_id": target.instrument_id,
                "listing_id": target.listing_id,
                "currency": assertion.currency,
                "origin": origin.origin,
                "period_end": assertion.period_end.isoformat(),
                "accounting_standards": assertion.accounting_standards,
                "by_period_end": {
                    end.isoformat(): {name: _s(values.get(name)) for name in CORROBORATED_FINANCIAL_FIELDS}
                    for end, values in sorted(assertion.by_period_end.items())
                },
            }
            for name in CORROBORATED_FINANCIAL_FIELDS:
                payload[name] = _s(assertion.values.get(name))
            found.append(
                Corroboration(
                    origin=origin.origin,
                    transaction_time=assertion.knowable_at,
                    record=NormalizedRecord(
                        payload=payload,
                        parser_version=origin.parser_version,
                        mapping_version=origin.mapping_version,
                    ),
                    confidence=origin.confidence,
                    raw=RawResponse(
                        body=assertion.raw_bytes,
                        source=origin.raw_source,
                        record_id=f"{origin.origin}:{target.ticker}:{assertion.period_end.isoformat()}",
                    ),
                    normalized_sha256=canonical_sha256(payload),
                )
            )
        return tuple(found)

    def _resolve_bundle_with_predecessor(self, target: SecTarget) -> FinancialFactsBundle | FetchFailure | None:
        try:
            bundle = self._fetcher(target.cik, target.cutoff, target.operating_branch)
            if bundle is not None and target.predecessor_cik is not None and not _asserts_income(bundle):
                fallback = self._fetcher(target.predecessor_cik, target.cutoff, target.operating_branch)
                if fallback is not None and _asserts_income(fallback):
                    if target.predecessor_signed and bundle.document is not None and fallback.document is not None:
                        merged = merge_company_facts(
                            [
                                (company_facts_record_id(target.cik), bundle.document),
                                (company_facts_record_id(target.predecessor_cik), fallback.document),
                            ],
                            self._ruleset,
                        )
                        return build_bundle(
                            merged,
                            target.cutoff,
                            target.operating_branch,
                            raw_bytes=bundle.raw_bytes,
                            ruleset=self._ruleset,
                        )
                    return fallback
            return bundle
        except (BudgetExhausted, CapacityExceeded):
            return FetchFailure(ObligationReasonCode.DEFERRED_CAPACITY)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        target = self._targets.get(work_item.work_item_id)
        if target is None:
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        bundle = self._resolve_bundle_with_predecessor(target)
        if isinstance(bundle, FetchFailure):
            return bundle
        if bundle is None:
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if bundle.knowable_at is not None and bundle.knowable_at.date() > target.cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)

        operating_period_stale = _periods_differ(bundle.operating_period_end, bundle.revenue_period_end)
        withheld = operating_period_stale or target.registry_unavailable
        gross_profit_value = None if withheld else bundle.gross_profit
        pre_provision_value = None if withheld else bundle.pre_provision_profit
        bank_net_revenue_value = None if withheld else bundle.bank_net_revenue

        knowable_at = bundle.knowable_at or knowable_instant(AsOfAxis.ACCESSION, target.cutoff)
        headcount, headcount_evidence, knowable_at = self._resolve_headcount_fact(target, knowable_at)
        payload: dict[str, Any] = {
            "issuer_id": target.issuer_id,
            "instrument_id": target.instrument_id,
            "listing_id": target.listing_id,
            "operating_branch": target.operating_branch.value,
            "currency": bundle.currency if bundle.currency is not None else _ruleset_currency(self._ruleset),
            "gross_profit": _s(gross_profit_value),
            "total_assets": _s(bundle.total_assets),
            "headcount": _s(headcount),
            "revenue": _s(bundle.revenue),
            "shares_outstanding": _s(bundle.shares_outstanding),
            "shares_basis": bundle.shares_basis,
            "pre_provision_profit": _s(pre_provision_value),
            "operating_period_end": None if withheld else _d(bundle.operating_period_end),
            "revenue_period_end": _d(bundle.revenue_period_end),
            "shares_period_end": _d(bundle.shares_period_end),
            "net_income": _s(bundle.net_income),
            "financial_assets": _s(bundle.financial_assets),
            "financial_returns": _s(bundle.financial_returns),
            "financial_basis": {
                "assets": bundle.financial_assets_basis,
                "returns": bundle.financial_returns_basis,
                "returns_is_proxy": bundle.financial_returns_is_proxy,
            },
            "net_income_by_period": {
                end.isoformat(): _s(value) for end, value in sorted(bundle.net_income_by_period.items())
            },
            "vintage": _payload_vintage(bundle, gross_profit_value, headcount_evidence, bank_net_revenue_value),
        }
        if target.operating_branch is OperatingBranch.FINANCIAL:
            payload.update(
                {
                    "bank_net_revenue": _s(bank_net_revenue_value),
                    "stockholders_equity": _s(bundle.stockholders_equity),
                    "preferred_stock_value": _s(bundle.preferred_stock_value),
                    "goodwill": _s(bundle.goodwill),
                    "intangible_assets_net_excluding_goodwill": _s(bundle.intangible_assets_net_excluding_goodwill),
                    "intangible_basis": bundle.intangible_basis,
                    "tangible_equity_period_end": _d(bundle.tangible_equity_period_end),
                }
            )
        if target.registry_unavailable:
            payload[UNAVAILABLE_PAYLOAD_KEY] = True
        payload.update(self._listing_ratio_fields(target, bundle))
        if operating_period_stale:
            payload["operating_period_stale"] = True
            payload["stale_operating_period_end"] = _d(bundle.operating_period_end)
        valid_from = (
            bundle.period_start or bundle.revenue_period_end or bundle.operating_period_end or knowable_at.date()
        )
        return FetchSuccess(
            raw=RawResponse(
                body=bundle.raw_bytes,
                source=DataSource.SEC,
                record_id=company_facts_record_id(target.cik),
            ),
            normalized_sha256=canonical_sha256(payload),
            valid_from=valid_from,
            transaction_time=knowable_at,
            record=NormalizedRecord(
                payload=payload, parser_version=PARSER_VERSION, mapping_version=self._mapping_version
            ),
            corroborations=self._corroborate(target),
        )

    def _listing_ratio_fields(self, target: SecTarget, bundle: FinancialFactsBundle) -> dict[str, Any]:
        from data_engine.datahub.standards.listing_unit_ratio import (
            PATTERN_PLAIN_SHARE,
            REASON_NO_ANNUAL_FILING,
            ListingUnitRatio,
        )

        if self._listing_ratio_reader is None or target.registry_unavailable:
            return {}
        ratio = self._listing_ratio_reader(target.cik, target.cutoff, target.ticker)
        if ratio.reason == REASON_NO_ANNUAL_FILING and target.predecessor_cik is not None:
            ratio = self._listing_ratio_reader(target.predecessor_cik, target.cutoff, target.ticker)
        if ratio.reason == REASON_NO_ANNUAL_FILING:
            domestic_10k = _domestic_10k_vintage(bundle)
            if domestic_10k is not None:
                filed_str = domestic_10k.get("filed")
                ratio = ListingUnitRatio(
                    Decimal(1),
                    PATTERN_PLAIN_SHARE,
                    accession=domestic_10k.get("accession"),
                    form=domestic_10k.get("form"),
                    filed=date.fromisoformat(filed_str) if isinstance(filed_str, str) and filed_str else None,
                )
        return ratio.payload_fields()

    def _resolve_headcount_fact(
        self, target: SecTarget, knowable_at: datetime
    ) -> tuple[Decimal | None, dict[str, Any] | None, datetime]:
        if self._headcount_extractor is None:
            return None, None, knowable_at
        fact = self._headcount_extractor(target.cik, target.cutoff)
        if fact is None and target.predecessor_cik is not None:
            fact = self._headcount_extractor(target.predecessor_cik, target.cutoff)
        if fact is None or fact.knowable_at.date() > target.cutoff:
            return None, None, knowable_at
        return fact.value, headcount_vintage(fact), max(knowable_at, fact.knowable_at)


def _domestic_10k_vintage(bundle: FinancialFactsBundle) -> Mapping[str, Any] | None:
    if bundle.document is None:
        return None
    for entry in bundle.vintages.values():
        if isinstance(entry, Mapping) and entry.get("form") in _FOREIGN_ANNUAL_FORMS:
            return None
    for key in ("shares_outstanding", "revenue", "gross_profit", "net_income", "total_assets"):
        candidate = bundle.vintages.get(key)
        if isinstance(candidate, Mapping) and candidate.get("form") in _DOMESTIC_10K_FORMS:
            return candidate
    return None


def _periods_differ(operating_end: object | None, revenue_end: object | None) -> bool:
    return operating_end is not None and revenue_end is not None and operating_end != revenue_end


def _fresh_shares(datum: _Datum | None, cutoff: date) -> _Datum | None:
    max_age_days = load_live_strategy_definition().eligibility.maximum_input_age_days
    if datum is None or (cutoff - datum.period_end).days > max_age_days:
        return None
    return datum


def _newest_period_end(*candidates: _Datum | None) -> date | None:
    ends = [datum.period_end for datum in candidates if datum is not None]
    return max(ends) if ends else None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _enrich_multi_class_shares(
    cik: int,
    facts: dict[str, Any],
    cutoff: date,
    branch: OperatingBranch,
    *,
    ruleset: ConceptMappingRuleset = DEFAULT_RULESET,
) -> FinancialFactsBundle | None:
    from data_engine.datahub.standards.filing_extraction import latest_periodic_filing, multi_class_share_entries
    from data_engine.sources import sec

    cutoff_dt = datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC)
    try:
        with sec.client() as http:
            document = latest_periodic_filing(cik, http=http, cutoff=cutoff_dt)
        if document is None:
            return None
        entries = multi_class_share_entries(document)
    except Exception:  # noqa: BLE001
        return None
    if not entries:
        return None
    dei = facts.setdefault("facts", {}).setdefault("dei", {})
    units = dei.setdefault("EntityCommonStockSharesOutstanding", {}).setdefault("units", {})
    units.setdefault("shares", []).extend(entries)
    enriched_bytes = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    return build_bundle(facts, cutoff, branch, raw_bytes=enriched_bytes, ruleset=ruleset)


def sec_financial_fetcher(
    cik: int,
    cutoff: date,
    branch: OperatingBranch,
    *,
    ruleset: ConceptMappingRuleset = DEFAULT_RULESET,
) -> FinancialFactsBundle:
    """Default fetcher backed by the source-gateway SEC company-facts client."""
    import httpx

    from data_engine.sources import sec

    try:
        body, facts = sec.fetch_company_facts_response(cik)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    bundle = build_bundle(facts, cutoff, branch, raw_bytes=body, ruleset=ruleset)
    if bundle.shares_outstanding is None:
        enriched = _enrich_multi_class_shares(cik, facts, cutoff, branch, ruleset=ruleset)
        if enriched is not None:
            return enriched
    return bundle


def _sec_ticker(ticker: str) -> str:
    return ticker.replace(".", "-")


class PredecessorResolution(int):
    """Integer CIK carrying whether the predecessor resolution was owner-signed."""

    signed: bool = False

    def __new__(cls, val: int, *, signed: bool = False) -> PredecessorResolution:
        obj = super().__new__(cls, val)
        obj.signed = signed
        return obj


def predecessor_ciks(
    connection: psycopg.Connection[Any],
    listing_ids: Sequence[str],
    issuer_by_listing: dict[str, str] | None = None,
    *,
    candidate_ids_by_listing: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, PredecessorResolution]:
    """Resolve each listing's predecessor company-facts CIK from signed registry or capture lineage."""
    from data_engine.datahub.canonical_issuer import signed_predecessor_ciks

    resolved: dict[str, PredecessorResolution] = {}
    if issuer_by_listing:
        for listing_id, cik in signed_predecessor_ciks(
            connection, issuer_by_listing, candidate_ids_by_key=candidate_ids_by_listing
        ).items():
            resolved[listing_id] = PredecessorResolution(int(cik), signed=True)

    if listing_ids:
        rows = connection.execute(
            """
            select distinct on (o.subject_id) o.subject_id, v.source_record_id
            from staging.capture_normalized_observations o
            join staging.capture_observation_payloads p on p.observation_id = o.observation_id
            join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
            where o.subject_id = any(%s)
              and o.semantic_type = 'financial-fact'
              and p.normalized_payload->>'revenue' is not null
              and v.source_record_id like 'companyfacts:CIK%%'
            order by o.subject_id, o.knowable_at desc
            """,
            (list(listing_ids),),
        ).fetchall()
        for subject_id, record_id in rows:
            digits = record_id.removeprefix("companyfacts:CIK")
            if digits.isdigit():
                resolved.setdefault(subject_id, PredecessorResolution(int(digits), signed=False))
    return resolved


def _candidate_ids_by_listing(context: RouteContext) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {}
    for subject_id, (issuer_id, _, listing_id, _) in context.coordinates.items():
        cids = candidates.setdefault(listing_id, [issuer_id])
        if context.raw_coordinates and subject_id in context.raw_coordinates:
            raw_issuer_id = context.raw_coordinates[subject_id][0]
            if raw_issuer_id not in cids:
                cids.append(raw_issuer_id)
    return candidates


def build_route(context: RouteContext, cells: Sequence[RouteCell]) -> SecFinancialFactAdapter:
    """Resolve CIKs, issuer branches, predecessor CIKs, and concept ruleset for one run."""
    from functools import partial

    from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
    from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor
    from data_engine.datahub.production_topt.issuer_registry import resolve_issuer_classifications
    from data_engine.datahub.production_topt.moomoo_origin import moomoo_financials_origin
    from data_engine.datahub.standards.listing_unit_ratio import CoverRatioReader
    from data_engine.datahub.standards.planner import resolve_issuer_cik
    from data_engine.sources import sec

    connection = context.connection
    tickers = {coordinate[3] for coordinate in context.coordinates.values()}
    cik_by_ticker: dict[str, int] = {}
    for issuer_id, _, _, ticker in context.coordinates.values():
        cik = resolve_issuer_cik(connection, issuer_id, as_of=context.cutoff_date, known_at=context.cutoff)
        if cik is not None:
            cik_by_ticker[ticker] = cik
    if context.raw_coordinates:
        for raw_issuer_id, _, _, ticker in context.raw_coordinates.values():
            if ticker not in cik_by_ticker and raw_issuer_id.startswith("issuer:cik:"):
                cik_by_ticker[ticker] = int(raw_issuer_id.removeprefix("issuer:cik:"))
    unresolved = sorted(tickers - set(cik_by_ticker))
    if unresolved:
        index = sec.ticker_cik_index()
        for ticker in unresolved:
            if _sec_ticker(ticker) in index:
                cik_by_ticker[ticker] = index[_sec_ticker(ticker)]
    missing = sorted(tickers - set(cik_by_ticker))
    if missing:
        raise LookupError(f"SEC ticker mapping does not cover: {', '.join(missing)}")

    registry = resolve_issuer_classifications(cik_by_ticker)
    classifications = registry.classifications
    listing_ids = [coordinate[2] for coordinate in context.coordinates.values()]
    issuer_by_listing = {coordinate[2]: coordinate[0] for coordinate in context.coordinates.values()}
    predecessors = (
        predecessor_ciks(
            connection,
            listing_ids,
            issuer_by_listing,
            candidate_ids_by_listing=_candidate_ids_by_listing(context),
        )
        if connection is not None
        else {}
    )
    targets: dict[str, SecTarget] = {}
    for cell in cells:
        cik = cik_by_ticker[cell.ticker]
        pred = predecessors.get(cell.listing_id)
        has_pred = pred is not None and pred != cik
        targets[cell.work_item_id] = SecTarget(
            cik=cik,
            cutoff=context.cutoff_date,
            issuer_id=cell.issuer_id,
            instrument_id=cell.instrument_id,
            listing_id=cell.listing_id,
            operating_branch=(
                classifications[cik].operating_branch if cik in classifications else OperatingBranch.NON_FINANCIAL
            ),
            registry_unavailable=cik in registry.unavailable,
            predecessor_cik=int(pred) if pred is not None and has_pred else None,
            predecessor_signed=getattr(pred, "signed", False) if pred is not None and has_pred else False,
            statement_filings=classifications[cik].statement_filings if cik in classifications else (),
            ticker=cell.ticker,
        )
    ruleset = resolve_ruleset(connection)
    second_origin = moomoo_financials_origin()
    return SecFinancialFactAdapter(
        targets,
        partial(sec_financial_fetcher, ruleset=ruleset),
        headcount_extractor=None if connection is None else PostgresHeadcountExtractor(connection),
        mapping_version=f"{MAPPING_VERSION}+{ruleset.content_sha256[:12]}",
        corroborating_origins=() if second_origin is None else (second_origin,),
        ruleset=ruleset,
        listing_ratio_reader=CoverRatioReader(),
    )
