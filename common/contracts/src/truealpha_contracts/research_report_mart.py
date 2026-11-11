"""Mart-backed `ResearchReadPort` implementation (#369, #42)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

from truealpha_contracts.access import AccessContext
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.issuer_modules_mart import (
    NO_SERVED_ROW,
    IssuerModuleReader,
    PostgresIssuerModuleReader,
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

_DEFAULT_STRATEGY_ID = _STRATEGY_FACTOR_VERSION = "large_model_value_v0"
_SUPPLY_CHAIN_TITLE = "Supply-chain exposure"
_ANALYST_TITLE = "Analyst consensus"
MART_READ_FAILED = "mart_read_failed"
ROW_AFTER_REPORT_CUTOFF = "served_row_after_report_cutoff"
PEG_NOT_RECORDED = "peg_not_recorded"
_ETF_DISPLAY_NAMES = {"etf:qqq": "Invesco QQQ Trust", "etf:ivv": "iShares Core S&P 500 ETF"}


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _decision_availability(decision: StrategyRunDecision) -> AvailabilityStatus:
    if decision.outcome is StrategyRunOutcome.EXCLUDED:
        return (
            AvailabilityStatus.LOW_CONFIDENCE
            if decision.exclusion_reason == "below_confidence_floor"
            else AvailabilityStatus.EXCLUDED
        )
    return AvailabilityStatus.AVAILABLE


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
    cutoff_iso = decision.cutoff_at.isoformat().replace("+00:00", "Z")
    trace = EvidenceTrace(reference_id=f"{source}:{corpus_sha256[:12]}:{decision.issuer_id}:{cutoff_iso}")
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=tuple(
            ResultValue(
                label=label,
                value=val,
                unit=unit,
                period=period,
                cutoff_at=decision.cutoff_at,
                availability=status if val is not None else AvailabilityStatus.UNAVAILABLE,
                confidence=decision.confidence,
                factor_version=_STRATEGY_FACTOR_VERSION,
                trace=trace,
            )
            for label, val, unit in labeled
        ),
        reason_codes=reason_codes,
    )


def _valuation_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    by_gp = decision.valuation_basis == "price_to_gross_profit"
    middle: tuple[tuple[str, str | None, str | None], ...] = (
        (
            ("current_price_to_gross_profit", _opt_str(decision.current_price_to_gross_profit), None),
            ("target_price_to_gross_profit", _opt_str(decision.target_price_to_gross_profit), None),
            ("current_price_to_sales", _opt_str(decision.current_price_to_sales), None),
        )
        if by_gp
        else (
            ("current_price_to_sales", _opt_str(decision.current_price_to_sales), None),
            ("target_price_to_sales", _opt_str(decision.target_price_to_sales), None),
        )
    )
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.VALUATION,
        "Valuation (three-tier P/GP)" if by_gp else "Valuation (three-tier P/S)",
        (
            ("tier", decision.tier.value if decision.tier else None, None),
            *middle,
            ("valuation_gap", _opt_str(decision.valuation_gap), None),
        ),
    )


def _peg_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    status = _decision_availability(decision)
    reasons = (PEG_NOT_RECORDED,) if decision.peg is None else ()
    if decision.peg is None and status is AvailabilityStatus.AVAILABLE:
        status = AvailabilityStatus.UNAVAILABLE
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.PEG_CONVENTIONS,
        "PEG",
        (("peg", _opt_str(decision.peg), None), ("peg_rank", _opt_str(decision.peg_rank), None)),
        status_override=status,
        reason_codes=reasons,
    )


def _strategy_summary_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    reasons: tuple[str, ...] = (
        (decision.exclusion_reason,)
        if decision.exclusion_reason is not None
        else (
            ("missing_audited_gross_profit_or_coordinate_join",)
            if decision.outcome is StrategyRunOutcome.EXCLUDED and source != "strategy_smoke_fixture"
            else ()
        )
    )
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.STRATEGY_SUMMARY,
        "Large-model-value strategy decision",
        (
            ("outcome", decision.outcome.value, None),
            ("eligible", str(decision.eligible).lower(), None),
            ("rank", _opt_str(decision.rank), None),
            ("target_weight", _opt_str(decision.target_weight), None),
        ),
        reason_codes=reasons,
    )


def _module_absent(section_kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    status = AvailabilityStatus.ERROR if reason == MART_READ_FAILED else AvailabilityStatus.UNAVAILABLE
    return ReportSection(section_kind=section_kind, title=title, availability=status, reason_codes=(reason,))


def _read_module_section(
    issuer_id: str,
    report_cutoff: datetime,
    section_kind: ReportSectionKind,
    title: str,
    table: str,
    fetch: Callable[[str], Any],
    fields: tuple[str, ...],
) -> ReportSection:
    try:
        row = fetch(issuer_id)
    except psycopg.Error:
        return _module_absent(section_kind, title, MART_READ_FAILED)
    if row is None:
        return _module_absent(section_kind, title, NO_SERVED_ROW)
    cutoff_at = datetime.fromisoformat(row.cutoff.replace("Z", "+00:00"))
    if cutoff_at > report_cutoff:
        return _module_absent(section_kind, title, ROW_AFTER_REPORT_CUTOFF)
    status = row.availability_status
    trace = EvidenceTrace(reference_id=f"{table}:{row.run_id}:{issuer_id}", materialized_output_id=row.run_id)
    period = cutoff_at.date().isoformat()
    conf = Decimal(row.confidence)
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=status,
        validation_status=row.factor_validation_status,
        results=tuple(
            ResultValue(
                label=field,
                value=_opt_str(getattr(row, field)),
                period=period,
                cutoff_at=cutoff_at,
                availability=status if getattr(row, field) is not None else AvailabilityStatus.UNAVAILABLE,
                confidence=conf,
                factor_version=row.extractor,
                trace=trace,
            )
            for field in fields
        ),
        reason_codes=row.reason_codes,
    )


def _subject_display_name(issuer_id: str, source: str, display_map: dict[str, tuple[str, str]] | None = None) -> str:
    if source == "strategy_smoke_fixture" and not display_map:
        return issuer_id
    if display_map and (entry := display_map.get(issuer_id) or display_map.get(issuer_id.upper())):
        return entry[1]
    return resolve_static_entity_display(issuer_id)[2]


def _operating_efficiency_section(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    return _decision_section(
        decision,
        source,
        corpus_sha256,
        ReportSectionKind.OPERATING_EFFICIENCY,
        "Operating efficiency (capital-adjusted labor efficiency)",
        (("capital_adjusted_labor_efficiency", _opt_str(decision.capital_adjusted_labor_efficiency), "USD"),),
    )


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
            _read_module_section(
                decision.issuer_id,
                decision.cutoff_at,
                ReportSectionKind.SUPPLY_CHAIN,
                _SUPPLY_CHAIN_TITLE,
                "mart.issuer_supply_chain_exposure",
                modules.supply_chain_exposure,
                ("exposure_score", "direct_partners", "suppliers_count", "customers_count", "max_partner_share"),
            ),
            _read_module_section(
                decision.issuer_id,
                decision.cutoff_at,
                ReportSectionKind.ANALYST_HISTORY,
                _ANALYST_TITLE,
                "mart.issuer_analyst_ratings",
                modules.analyst_ratings,
                ("consensus_rating", "target_price", "analysts_count", "buy_count", "hold_count", "sell_count"),
            ),
            _strategy_summary_section(decision, source, corpus_sha256),
        ),
    )


def _missing_subject(entity_id: str, reason: str) -> ReportSubject:
    return ReportSubject(
        subject_id=entity_id,
        display_name=entity_id,
        sections=(_module_absent(ReportSectionKind.STRATEGY_SUMMARY, "Large-model-value strategy decision", reason),),
    )


def _etf_subject(entity_id: str, request: ResearchReportRequest) -> ReportSubject:
    return ReportSubject(
        subject_id=entity_id,
        display_name=_ETF_DISPLAY_NAMES.get(entity_id, entity_id),
        sections=(
            ReportSection(
                section_kind=ReportSectionKind.ETF_VIRTUAL_COMPANY,
                title="ETF virtual company",
                availability=AvailabilityStatus.UNAVAILABLE,
                results=(
                    ResultValue(
                        label="virtual_company_fundamentals",
                        value=None,
                        cutoff_at=request.cutoff_at,
                        availability=AvailabilityStatus.UNAVAILABLE,
                    ),
                ),
                reason_codes=("etf_virtual_company_module_not_materialized",),
            ),
            _module_absent(ReportSectionKind.VALUATION, "Valuation (three-tier P/S)", "etf_valuation_not_materialized"),
        ),
    )


def _build_decision_lookup(decisions: list[StrategyRunDecision]) -> dict[str, StrategyRunDecision]:
    by_issuer: dict[str, StrategyRunDecision] = {}
    for d in decisions:
        by_issuer[d.issuer_id] = d
        by_issuer.setdefault(d.issuer_id.upper(), d)
        if ticker := resolve_static_entity_display(d.issuer_id)[1]:
            by_issuer.setdefault(ticker.upper(), d)
    return by_issuer


def _match_target_decisions(
    target_entity_ids: tuple[str, ...] | list[str],
    by_issuer: dict[str, StrategyRunDecision],
    database_url: str | None,
) -> list[tuple[str, StrategyRunDecision | None]]:
    unmatched = [e for e in target_entity_ids if by_issuer.get(e) is None and by_issuer.get(e.upper()) is None]
    resolved = resolve_issuer_id_candidates_batch(database_url, unmatched) if unmatched and database_url else {}
    out: list[tuple[str, StrategyRunDecision | None]] = []
    for eid in target_entity_ids:
        matched = by_issuer.get(eid) or by_issuer.get(eid.upper())
        if matched is None:
            matched = next(
                (m for c in resolved.get(eid.strip(), ()) if (m := by_issuer.get(c) or by_issuer.get(c.upper()))),
                None,
            )
        out.append((eid, matched))
    return out


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

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]:
        if request.report_kind is ResearchReportKind.ETF:
            return tuple(_etf_subject(eid, request) for eid in request.target_entity_ids)
        res = self._strategy_repository.get_latest(
            strategy_id=request.strategy_id or _DEFAULT_STRATEGY_ID, context=context
        )
        report = res if isinstance(res, StrategyRunReport) else None
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
            ordered = sorted(
                decisions, key=lambda d: (1, 0, d.issuer_id) if d.rank is None else (0, d.rank, d.issuer_id)
            )
            targets = [e for e in request.target_entity_ids if e.strip() and not e.strip().lower().startswith("theme:")]
            if targets:
                allowed = {
                    m.issuer_id
                    for _, m in _match_target_decisions(targets, by_issuer, self._database_url)
                    if m is not None
                }
                if allowed:
                    ordered = [d for d in ordered if d.issuer_id in allowed]
            if not ordered:
                return tuple(_missing_subject(eid, "strategy_run_unavailable") for eid in request.target_entity_ids)
            return tuple(
                ReportSubject(
                    subject_id=d.issuer_id,
                    display_name=_subject_display_name(d.issuer_id, report.source, display_map),
                    rank=d.rank,
                    sections=(
                        _strategy_summary_section(d, report.source, report.corpus_sha256),
                        _valuation_section(d, report.source, report.corpus_sha256),
                    ),
                )
                for d in ordered
            )
        return tuple(
            _missing_subject(eid, "subject_not_in_strategy_run")
            if matched is None
            else _company_subject(matched, report.source, report.corpus_sha256, self._issuer_modules, display_map)
            for eid, matched in _match_target_decisions(request.target_entity_ids, by_issuer, self._database_url)
        )
