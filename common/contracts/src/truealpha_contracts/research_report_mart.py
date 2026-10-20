"""Mart-backed `ResearchReadPort`, the one research-report reader — see #369, #42.

The reader selects already-materialized values and computes nothing. A company subject has
seven sections, each from its own mart output:

- operating efficiency, valuation, PEG and the strategy summary: the strategy decision of
  the requested cutoff (`PostgresStrategyRunRepository`, the governed run first, #575);
- supply chain (module 3) and analyst history (module 4): the served-head row of
  `mart.issuer_supply_chain_exposure` and `mart.issuer_analyst_ratings`
  (`issuer_modules_mart`), the rows the App shows on `/research/entities/<id>`.

Until #42 this class subclassed `FixtureResearchReadRepository` and inherited its constant
"not materialized" sections for PEG, supply chain and analyst history, although the App
read all three from mart. AGENTS.md rule 6 keeps fixture code out of deployed paths;
`common/runtime/tests/test_no_fixture_in_deployed_code.py` now fails when a module that a
deployed entry point imports takes a name from a `*_fixture` module or subclasses a
`Fixture*` class.

Mapping a materialized outcome to a display availability status is a lookup over an
already-computed result, not a new metric, and it lives here on the read side, never in
the builder.

`common/contracts` classes take their database URL as an explicit argument; resolving which
`DATABASE_URL` applies is the caller's job (an MCP tool's `settings.database_url`, or a
test's own database).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import psycopg

from truealpha_contracts.access import AccessContext
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.issuer_modules_mart import (
    AnalystRatings,
    IssuerModuleReader,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
)
from truealpha_contracts.research_report import (
    EvidenceTrace,
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReportKind,
    ResearchReportRequest,
    ResultValue,
)
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReadRepository,
    StrategyRunReport,
)
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository

_DEFAULT_STRATEGY_ID = "large_model_value_v0"
_STRATEGY_FACTOR_VERSION = "large_model_value_v0"

_SUPPLY_CHAIN_TITLE = "Supply-chain exposure"
_ANALYST_TITLE = "Analyst consensus"
#: The served head holds no row of the module for this issuer.
NO_SERVED_ROW = "no_row_in_served_head"
#: The module read failed; the section says so instead of reading as "no row".
MART_READ_FAILED = "mart_read_failed"
#: The served row is later than the report's cutoff: it was not knowable at the cutoff.
ROW_AFTER_REPORT_CUTOFF = "served_row_after_report_cutoff"
#: The strategy decision of the cutoff records no PEG value.
PEG_NOT_RECORDED = "peg_not_recorded"

_ETF_DISPLAY_NAMES: dict[str, str] = {
    "etf:qqq": "Invesco QQQ Trust",
    "etf:ivv": "iShares Core S&P 500 ETF",
}


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decision_availability(decision: StrategyRunDecision) -> AvailabilityStatus:
    """Maps an already-materialized decision to a display availability status."""
    if decision.outcome is StrategyRunOutcome.EXCLUDED:
        if decision.exclusion_reason == "below_confidence_floor":
            return AvailabilityStatus.LOW_CONFIDENCE
        return AvailabilityStatus.EXCLUDED
    return AvailabilityStatus.AVAILABLE


def _value_availability(value: str | None, section_status: AvailabilityStatus) -> AvailabilityStatus:
    return section_status if value is not None else AvailabilityStatus.UNAVAILABLE


def _trace(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> EvidenceTrace:
    # Full cutoff_at, not just its date: truncating to a bare date would collide across
    # multiple same-day cutoffs (Copilot review on #387; mirrored here to keep this and
    # research-read.ts's traceId() byte-identical, per #347's parity contract). `source`
    # (the strategy report's own StrategyRunReport.source, not a hardcoded literal — #369)
    # so a mart-backed report's trace does not misrepresent itself as fixture-derived.
    cutoff = decision.cutoff_at.isoformat().replace("+00:00", "Z")
    return EvidenceTrace(
        reference_id=f"{source}:{corpus_sha256[:12]}:{decision.issuer_id}:{cutoff}",
    )


def _result(
    *,
    label: str,
    value: str | None,
    decision: StrategyRunDecision,
    section_status: AvailabilityStatus,
    source: str,
    corpus_sha256: str,
    unit: str | None = None,
) -> ResultValue:
    return ResultValue(
        label=label,
        value=value,
        unit=unit,
        period=decision.cutoff_at.date().isoformat(),
        cutoff_at=decision.cutoff_at,
        availability=_value_availability(value, section_status),
        confidence=decision.confidence,
        factor_version=_STRATEGY_FACTOR_VERSION,
        trace=_trace(decision, source, corpus_sha256),
    )


def _operating_efficiency_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    value = _decimal_str(decision.capital_adjusted_labor_efficiency)
    status = _decision_availability(decision)
    # The section's own availability mirrors the decision (LOW_CONFIDENCE/EXCLUDED must
    # stay visible even when this one field is null) — matching every other section
    # builder here. A null value's own downgrade to UNAVAILABLE happens per-result inside
    # _result()/_value_availability, not at the section level (Copilot review on #383).
    return ReportSection(
        section_kind=ReportSectionKind.OPERATING_EFFICIENCY,
        title="Operating efficiency (capital-adjusted labor efficiency)",
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=(
            _result(
                label="capital_adjusted_labor_efficiency",
                value=value,
                decision=decision,
                section_status=status,
                source=source,
                corpus_sha256=corpus_sha256,
                unit="USD",
            ),
        ),
    )


def _valuation_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    status = _decision_availability(decision)
    tier_value = decision.tier.value if decision.tier is not None else None
    # #1235: the gap divides the multiple of the decision's definition. A 1.0.0 decision
    # reports its P/GP pair and still records P/S; a 0.1.0 decision reports its P/S pair.
    by_gross_profit = decision.valuation_basis == "price_to_gross_profit"
    labeled: tuple[tuple[str, str | None], ...] = (("tier", tier_value),)
    if by_gross_profit:
        labeled += (
            ("current_price_to_gross_profit", _decimal_str(decision.current_price_to_gross_profit)),
            ("target_price_to_gross_profit", _decimal_str(decision.target_price_to_gross_profit)),
            ("current_price_to_sales", _decimal_str(decision.current_price_to_sales)),
        )
    else:
        labeled += (
            ("current_price_to_sales", _decimal_str(decision.current_price_to_sales)),
            ("target_price_to_sales", _decimal_str(decision.target_price_to_sales)),
        )
    labeled += (("valuation_gap", _decimal_str(decision.valuation_gap)),)
    results = tuple(
        _result(
            label=label,
            value=value,
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        )
        for label, value in labeled
    )
    return ReportSection(
        section_kind=ReportSectionKind.VALUATION,
        title="Valuation (three-tier P/GP)" if by_gross_profit else "Valuation (three-tier P/S)",
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=results,
    )


def _peg_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    """Module 1: the PEG and its own rank, as the strategy run recorded them (#284).

    The PEG rank is separate from the strategy rank: PEG does not take part in selection.
    A decision without a PEG makes the section unavailable, unless the decision itself is
    excluded or low-confidence, which stays visible.
    """
    status = _decision_availability(decision)
    reasons: tuple[str, ...] = ()
    if decision.peg is None:
        reasons = (PEG_NOT_RECORDED,)
        if status is AvailabilityStatus.AVAILABLE:
            status = AvailabilityStatus.UNAVAILABLE
    results = tuple(
        _result(
            label=label,
            value=value,
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        )
        for label, value in (
            ("peg", _decimal_str(decision.peg)),
            ("peg_rank", None if decision.peg_rank is None else str(decision.peg_rank)),
        )
    )
    return ReportSection(
        section_kind=ReportSectionKind.PEG_CONVENTIONS,
        title="PEG",
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=results,
        reason_codes=reasons,
    )


def _strategy_summary_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    status = _decision_availability(decision)
    reason_codes = (decision.exclusion_reason,) if decision.exclusion_reason is not None else ()
    results = (
        _result(
            label="outcome",
            value=decision.outcome.value,
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="eligible",
            value=str(decision.eligible).lower(),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="rank",
            value=None if decision.rank is None else str(decision.rank),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="target_weight",
            value=_decimal_str(decision.target_weight),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
    )
    return ReportSection(
        section_kind=ReportSectionKind.STRATEGY_SUMMARY,
        title="Large-model-value strategy decision",
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=results,
        reason_codes=reason_codes,
    )


def _module_section(
    section_kind: ReportSectionKind,
    title: str,
    table: str,
    issuer_id: str,
    row: SupplyChainExposure | AnalystRatings,
    labeled: tuple[tuple[str, str | None], ...],
    report_cutoff: datetime,
) -> ReportSection:
    """One served module row as a section. Every value keeps the row's own cutoff,
    confidence, extractor and run, so a reader sees where it came from. A row later than
    the report's cutoff is not shown: the report is anchored to what was knowable then."""
    cutoff_at = datetime.fromisoformat(row.cutoff.replace("Z", "+00:00"))
    if cutoff_at > report_cutoff:
        return _module_absent(section_kind, title, ROW_AFTER_REPORT_CUTOFF)
    status = row.availability_status
    trace = EvidenceTrace(
        reference_id=f"{table}:{row.run_id}:{issuer_id}",
        materialized_output_id=row.run_id,
    )
    results = tuple(
        ResultValue(
            label=label,
            value=value,
            period=cutoff_at.date().isoformat(),
            cutoff_at=cutoff_at,
            availability=_value_availability(value, status),
            confidence=Decimal(row.confidence),
            factor_version=row.extractor,
            trace=trace,
        )
        for label, value in labeled
    )
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=status,
        validation_status=row.factor_validation_status,
        results=results,
        reason_codes=row.reason_codes,
    )


def _module_absent(section_kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    status = AvailabilityStatus.ERROR if reason == MART_READ_FAILED else AvailabilityStatus.UNAVAILABLE
    return ReportSection(section_kind=section_kind, title=title, availability=status, reason_codes=(reason,))


def _supply_chain_section(issuer_id: str, report_cutoff: datetime, modules: IssuerModuleReader) -> ReportSection:
    try:
        row = modules.supply_chain_exposure(issuer_id)
    except psycopg.Error:
        return _module_absent(ReportSectionKind.SUPPLY_CHAIN, _SUPPLY_CHAIN_TITLE, MART_READ_FAILED)
    if row is None:
        return _module_absent(ReportSectionKind.SUPPLY_CHAIN, _SUPPLY_CHAIN_TITLE, NO_SERVED_ROW)
    return _module_section(
        ReportSectionKind.SUPPLY_CHAIN,
        _SUPPLY_CHAIN_TITLE,
        "mart.issuer_supply_chain_exposure",
        issuer_id,
        row,
        (
            ("exposure_score", row.exposure_score),
            ("direct_partners", str(row.direct_partners)),
            ("suppliers_count", str(row.suppliers_count)),
            ("customers_count", str(row.customers_count)),
            ("max_partner_share", row.max_partner_share),
        ),
        report_cutoff,
    )


def _analyst_section(issuer_id: str, report_cutoff: datetime, modules: IssuerModuleReader) -> ReportSection:
    try:
        row = modules.analyst_ratings(issuer_id)
    except psycopg.Error:
        return _module_absent(ReportSectionKind.ANALYST_HISTORY, _ANALYST_TITLE, MART_READ_FAILED)
    if row is None:
        return _module_absent(ReportSectionKind.ANALYST_HISTORY, _ANALYST_TITLE, NO_SERVED_ROW)
    return _module_section(
        ReportSectionKind.ANALYST_HISTORY,
        _ANALYST_TITLE,
        "mart.issuer_analyst_ratings",
        issuer_id,
        row,
        (
            ("consensus_rating", row.consensus_rating),
            ("target_price", row.target_price),
            ("analysts_count", str(row.analysts_count)),
            ("buy_count", str(row.buy_count)),
            ("hold_count", str(row.hold_count)),
            ("sell_count", str(row.sell_count)),
        ),
        report_cutoff,
    )


def _unavailable_section(section_kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=AvailabilityStatus.UNAVAILABLE,
        reason_codes=(reason,),
    )


def _company_subject(
    decision: StrategyRunDecision, source: str, corpus_sha256: str, modules: IssuerModuleReader
) -> ReportSubject:
    sections = (
        _operating_efficiency_section(decision, source, corpus_sha256),
        _valuation_section(decision, source, corpus_sha256),
        _peg_section(decision, source, corpus_sha256),
        _supply_chain_section(decision.issuer_id, decision.cutoff_at, modules),
        _analyst_section(decision.issuer_id, decision.cutoff_at, modules),
        _strategy_summary_section(decision, source, corpus_sha256),
    )
    return ReportSubject(subject_id=decision.issuer_id, display_name=decision.issuer_id, sections=sections)


def _ranking_subject(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSubject:
    return ReportSubject(
        subject_id=decision.issuer_id,
        display_name=decision.issuer_id,
        rank=decision.rank,
        sections=(
            _strategy_summary_section(decision, source, corpus_sha256),
            _valuation_section(decision, source, corpus_sha256),
        ),
    )


def _missing_subject(entity_id: str, reason: str) -> ReportSubject:
    return ReportSubject(
        subject_id=entity_id,
        display_name=entity_id,
        sections=(
            _unavailable_section(ReportSectionKind.STRATEGY_SUMMARY, "Large-model-value strategy decision", reason),
        ),
    )


def _etf_subject(entity_id: str, request: ResearchReportRequest) -> ReportSubject:
    unavailable = ResultValue(
        label="virtual_company_fundamentals",
        value=None,
        cutoff_at=request.cutoff_at,
        availability=AvailabilityStatus.UNAVAILABLE,
    )
    return ReportSubject(
        subject_id=entity_id,
        display_name=_ETF_DISPLAY_NAMES.get(entity_id, entity_id),
        sections=(
            ReportSection(
                section_kind=ReportSectionKind.ETF_VIRTUAL_COMPANY,
                title="ETF virtual company",
                availability=AvailabilityStatus.UNAVAILABLE,
                results=(unavailable,),
                reason_codes=("etf_virtual_company_module_not_materialized",),
            ),
            _unavailable_section(
                ReportSectionKind.VALUATION,
                "Valuation (three-tier P/S)",
                "etf_valuation_not_materialized",
            ),
        ),
    )


def _ranking_sort_key(decision: StrategyRunDecision) -> tuple[int, int, str]:
    # Ranked members first (ascending rank), then unranked by issuer id.
    if decision.rank is None:
        return (1, 0, decision.issuer_id)
    return (0, decision.rank, decision.issuer_id)


class MartResearchReadRepository:
    """Reads already-materialized research sections from mart.

    `database_url` builds both sources: `PostgresStrategyRunRepository` (#362) and
    `PostgresIssuerModuleReader` (#42). A test injects both sources instead; there is no
    default source and no fixture behind a flag."""

    provenance_label = "mart:research_report.v1"

    def __init__(
        self,
        *,
        database_url: str | None = None,
        strategy_repository: StrategyRunReadRepository | None = None,
        issuer_modules: IssuerModuleReader | None = None,
    ) -> None:
        if database_url is not None:
            strategy_repository = strategy_repository or PostgresStrategyRunRepository(database_url=database_url)
            issuer_modules = issuer_modules or PostgresIssuerModuleReader(database_url=database_url)
        if strategy_repository is None or issuer_modules is None:
            raise TypeError(
                "MartResearchReadRepository requires database_url, or both strategy_repository and issuer_modules"
            )
        self._strategy_repository = strategy_repository
        self._issuer_modules = issuer_modules

    def _strategy_report(self, request: ResearchReportRequest, context: AccessContext) -> StrategyRunReport | None:
        strategy_id = request.strategy_id if request.strategy_id is not None else _DEFAULT_STRATEGY_ID
        result = self._strategy_repository.get_latest(strategy_id=strategy_id, context=context)
        return result if isinstance(result, StrategyRunReport) else None

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]:
        if request.report_kind is ResearchReportKind.ETF:
            return tuple(_etf_subject(entity_id, request) for entity_id in request.target_entity_ids)

        report = self._strategy_report(request, context)
        if report is None:
            return tuple(
                _missing_subject(entity_id, "strategy_run_unavailable") for entity_id in request.target_entity_ids
            )
        decisions = [decision for decision in report.decisions if decision.cutoff_at == request.cutoff_at]

        if request.report_kind is ResearchReportKind.THEME_RANKING:
            ordered = sorted(decisions, key=_ranking_sort_key)
            return tuple(_ranking_subject(decision, report.source, report.corpus_sha256) for decision in ordered)

        by_issuer = {decision.issuer_id: decision for decision in decisions}
        subjects: list[ReportSubject] = []
        for entity_id in request.target_entity_ids:
            decision = by_issuer.get(entity_id)
            if decision is None:
                subjects.append(_missing_subject(entity_id, "subject_not_in_strategy_run"))
            else:
                subjects.append(_company_subject(decision, report.source, report.corpus_sha256, self._issuer_modules))
        return tuple(subjects)
