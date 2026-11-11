"""Mart-backed `ResearchReadPort` implementation (#369, #42)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import psycopg

from truealpha_contracts.access import AccessContext
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.issuer_modules_mart import (
    NO_SERVED_ROW,
    AnalystRatings,
    IssuerModuleReader,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
    load_entity_display_map,
    resolve_issuer_id_candidates_batch,
    resolve_static_entity_display,
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
MART_READ_FAILED = "mart_read_failed"
ROW_AFTER_REPORT_CUTOFF = "served_row_after_report_cutoff"
PEG_NOT_RECORDED = "peg_not_recorded"

_ETF_DISPLAY_NAMES: dict[str, str] = {
    "etf:qqq": "Invesco QQQ Trust",
    "etf:ivv": "iShares Core S&P 500 ETF",
}


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decision_availability(decision: StrategyRunDecision) -> AvailabilityStatus:
    if decision.outcome is StrategyRunOutcome.EXCLUDED:
        return (
            AvailabilityStatus.LOW_CONFIDENCE
            if decision.exclusion_reason == "below_confidence_floor"
            else AvailabilityStatus.EXCLUDED
        )
    return AvailabilityStatus.AVAILABLE


def _value_availability(value: str | None, section_status: AvailabilityStatus) -> AvailabilityStatus:
    return section_status if value is not None else AvailabilityStatus.UNAVAILABLE


def _trace(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> EvidenceTrace:
    cutoff = decision.cutoff_at.isoformat().replace("+00:00", "Z")
    return EvidenceTrace(reference_id=f"{source}:{corpus_sha256[:12]}:{decision.issuer_id}:{cutoff}")


def _decision_section(
    decision: StrategyRunDecision,
    source: str,
    corpus_sha256: str,
    section_kind: ReportSectionKind,
    title: str,
    labeled: tuple[tuple[str, str | None, str | None], ...],
    *,
    status_override: AvailabilityStatus | None = None,
    reason_codes: tuple[str, ...] = (),
) -> ReportSection:
    status = status_override or _decision_availability(decision)
    period = decision.cutoff_at.date().isoformat()
    trace = _trace(decision, source, corpus_sha256)
    results = tuple(
        ResultValue(
            label=label,
            value=value,
            unit=unit,
            period=period,
            cutoff_at=decision.cutoff_at,
            availability=_value_availability(value, status),
            confidence=decision.confidence,
            factor_version=_STRATEGY_FACTOR_VERSION,
            trace=trace,
        )
        for label, value, unit in labeled
    )
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=results,
        reason_codes=reason_codes,
    )


def _operating_efficiency_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.OPERATING_EFFICIENCY,
        "Operating efficiency (capital-adjusted labor efficiency)",
        (("capital_adjusted_labor_efficiency", _decimal_str(decision.capital_adjusted_labor_efficiency), "USD"),),
    )


def _valuation_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    by_gp = decision.valuation_basis == "price_to_gross_profit"
    middle: tuple[tuple[str, str | None, str | None], ...] = (
        (
            ("current_price_to_gross_profit", _decimal_str(decision.current_price_to_gross_profit), None),
            ("target_price_to_gross_profit", _decimal_str(decision.target_price_to_gross_profit), None),
            ("current_price_to_sales", _decimal_str(decision.current_price_to_sales), None),
        )
        if by_gp
        else (
            ("current_price_to_sales", _decimal_str(decision.current_price_to_sales), None),
            ("target_price_to_sales", _decimal_str(decision.target_price_to_sales), None),
        )
    )
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.VALUATION,
        "Valuation (three-tier P/GP)" if by_gp else "Valuation (three-tier P/S)",
        (
            ("tier", decision.tier.value if decision.tier is not None else None, None),
            *middle,
            ("valuation_gap", _decimal_str(decision.valuation_gap), None),
        ),
    )


def _peg_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    status = _decision_availability(decision)
    reasons: tuple[str, ...] = ()
    if decision.peg is None:
        reasons = (PEG_NOT_RECORDED,)
        if status is AvailabilityStatus.AVAILABLE:
            status = AvailabilityStatus.UNAVAILABLE
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.PEG_CONVENTIONS,
        "PEG",
        (
            ("peg", _decimal_str(decision.peg), None),
            ("peg_rank", None if decision.peg_rank is None else str(decision.peg_rank), None),
        ),
        status_override=status,
        reason_codes=reasons,
    )


def _strategy_summary_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    if decision.exclusion_reason is not None:
        reasons: tuple[str, ...] = (decision.exclusion_reason,)
    elif decision.outcome is StrategyRunOutcome.EXCLUDED and source != "strategy_smoke_fixture":
        reasons = ("missing_audited_gross_profit_or_coordinate_join",)
    else:
        reasons = ()
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.STRATEGY_SUMMARY,
        "Large-model-value strategy decision",
        (
            ("outcome", decision.outcome.value, None),
            ("eligible", str(decision.eligible).lower(), None),
            ("rank", None if decision.rank is None else str(decision.rank), None),
            ("target_weight", _decimal_str(decision.target_weight), None),
        ),
        reason_codes=reasons,
    )


def _module_absent(section_kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    status = AvailabilityStatus.ERROR if reason == MART_READ_FAILED else AvailabilityStatus.UNAVAILABLE
    return ReportSection(section_kind=section_kind, title=title, availability=status, reason_codes=(reason,))


def _module_section(
    section_kind: ReportSectionKind,
    title: str,
    table: str,
    issuer_id: str,
    row: SupplyChainExposure | AnalystRatings,
    labeled: tuple[tuple[str, str | None], ...],
    report_cutoff: datetime,
) -> ReportSection:
    cutoff_at = datetime.fromisoformat(row.cutoff.replace("Z", "+00:00"))
    if cutoff_at > report_cutoff:
        return _module_absent(section_kind, title, ROW_AFTER_REPORT_CUTOFF)
    status = row.availability_status
    trace = EvidenceTrace(reference_id=f"{table}:{row.run_id}:{issuer_id}", materialized_output_id=row.run_id)
    period = cutoff_at.date().isoformat()
    conf = Decimal(row.confidence)
    results = tuple(
        ResultValue(
            label=label,
            value=value,
            period=period,
            cutoff_at=cutoff_at,
            availability=_value_availability(value, status),
            confidence=conf,
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


def _subject_display_name(issuer_id: str, source: str, display_map: dict[str, tuple[str, str]] | None = None) -> str:
    if source == "strategy_smoke_fixture" and not display_map:
        return issuer_id
    if display_map:
        entry = display_map.get(issuer_id) or display_map.get(issuer_id.upper())
        if entry is not None:
            return entry[1]
    return resolve_static_entity_display(issuer_id)[2]


def _company_subject(
    decision: StrategyRunDecision,
    source: str,
    corpus_sha256: str,
    modules: IssuerModuleReader,
    display_map: dict[str, tuple[str, str]] | None = None,
) -> ReportSubject:
    return ReportSubject(
        subject_id=decision.issuer_id,
        display_name=_subject_display_name(decision.issuer_id, source, display_map),
        sections=(
            _operating_efficiency_section(decision, source, corpus_sha256),
            _valuation_section(decision, source, corpus_sha256),
            _peg_section(decision, source, corpus_sha256),
            _supply_chain_section(decision.issuer_id, decision.cutoff_at, modules),
            _analyst_section(decision.issuer_id, decision.cutoff_at, modules),
            _strategy_summary_section(decision, source, corpus_sha256),
        ),
    )


def _ranking_subject(
    decision: StrategyRunDecision,
    source: str,
    corpus_sha256: str,
    display_map: dict[str, tuple[str, str]] | None = None,
) -> ReportSubject:
    return ReportSubject(
        subject_id=decision.issuer_id,
        display_name=_subject_display_name(decision.issuer_id, source, display_map),
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
        sections=(_module_absent(ReportSectionKind.STRATEGY_SUMMARY, "Large-model-value strategy decision", reason),),
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
            _module_absent(ReportSectionKind.VALUATION, "Valuation (three-tier P/S)", "etf_valuation_not_materialized"),
        ),
    )


def _ranking_sort_key(decision: StrategyRunDecision) -> tuple[int, int, str]:
    return (1, 0, decision.issuer_id) if decision.rank is None else (0, decision.rank, decision.issuer_id)


def _build_decision_lookup(decisions: list[StrategyRunDecision]) -> dict[str, StrategyRunDecision]:
    by_issuer: dict[str, StrategyRunDecision] = {}
    for decision in decisions:
        by_issuer[decision.issuer_id] = decision
        by_issuer.setdefault(decision.issuer_id.upper(), decision)
        ticker = resolve_static_entity_display(decision.issuer_id)[1]
        if ticker is not None:
            by_issuer.setdefault(ticker.upper(), decision)
    return by_issuer


def _match_target_decisions(
    target_entity_ids: tuple[str, ...] | list[str],
    by_issuer: dict[str, StrategyRunDecision],
    database_url: str | None,
) -> list[tuple[str, StrategyRunDecision | None]]:
    unmatched = [
        entity_id
        for entity_id in target_entity_ids
        if by_issuer.get(entity_id) is None and by_issuer.get(entity_id.upper()) is None
    ]
    resolved_batch = (
        resolve_issuer_id_candidates_batch(database_url, unmatched) if unmatched and database_url is not None else {}
    )
    results: list[tuple[str, StrategyRunDecision | None]] = []
    for entity_id in target_entity_ids:
        matched = by_issuer.get(entity_id) or by_issuer.get(entity_id.upper())
        if matched is None:
            for candidate in resolved_batch.get(entity_id.strip(), ()):
                matched = by_issuer.get(candidate) or by_issuer.get(candidate.upper())
                if matched is not None:
                    break
        results.append((entity_id, matched))
    return results


class MartResearchReadRepository:
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
        self._database_url = database_url
        self._strategy_repository = strategy_repository
        self._issuer_modules = issuer_modules

    def _strategy_report(self, request: ResearchReportRequest, context: AccessContext) -> StrategyRunReport | None:
        strategy_id = request.strategy_id or _DEFAULT_STRATEGY_ID
        result = self._strategy_repository.get_latest(strategy_id=strategy_id, context=context)
        return result if isinstance(result, StrategyRunReport) else None

    def _load_theme_ranking_subjects(
        self,
        decisions: list[StrategyRunDecision],
        by_issuer: dict[str, StrategyRunDecision],
        request: ResearchReportRequest,
        report: StrategyRunReport,
        display_map: dict[str, tuple[str, str]] | None,
    ) -> tuple[ReportSubject, ...]:
        ordered = sorted(decisions, key=_ranking_sort_key)
        specific_targets = [
            eid for eid in request.target_entity_ids if eid.strip() and not eid.strip().lower().startswith("theme:")
        ]
        if specific_targets:
            matched_pairs = _match_target_decisions(specific_targets, by_issuer, self._database_url)
            allowed_ids = {m.issuer_id for _, m in matched_pairs if m is not None}
            if allowed_ids:
                ordered = [d for d in ordered if d.issuer_id in allowed_ids]
        if not ordered:
            return tuple(_missing_subject(eid, "strategy_run_unavailable") for eid in request.target_entity_ids)
        return tuple(_ranking_subject(d, report.source, report.corpus_sha256, display_map) for d in ordered)

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]:
        if request.report_kind is ResearchReportKind.ETF:
            return tuple(_etf_subject(entity_id, request) for entity_id in request.target_entity_ids)
        report = self._strategy_report(request, context)
        if report is None:
            return tuple(_missing_subject(eid, "strategy_run_unavailable") for eid in request.target_entity_ids)
        decisions = [d for d in report.decisions if d.cutoff_at == request.cutoff_at]
        display_map = (
            load_entity_display_map(self._database_url)
            if (self._database_url is not None or report.source != "strategy_smoke_fixture")
            else None
        )
        by_issuer = _build_decision_lookup(decisions)
        if request.report_kind is ResearchReportKind.THEME_RANKING:
            return self._load_theme_ranking_subjects(decisions, by_issuer, request, report, display_map)
        return tuple(
            _missing_subject(eid, "subject_not_in_strategy_run")
            if matched is None
            else _company_subject(matched, report.source, report.corpus_sha256, self._issuer_modules, display_map)
            for eid, matched in _match_target_decisions(request.target_entity_ids, by_issuer, self._database_url)
        )
