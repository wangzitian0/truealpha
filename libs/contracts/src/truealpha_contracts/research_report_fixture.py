"""Fixture-backed `ResearchReadPort` — see #369.

Provisional read side for the research-report builder. `#41` (the stable seven-module
mart read contract) is still open, so this repository reproduces already-materialized
values from the checked-in strategy-run fixture (`strategy_run_preview.v1.json`, the same
bytes `#347`'s `FixtureStrategyRunRepository` reads) rather than querying `mart`. The
company and theme/ranking reports therefore reproduce the MCP strategy-run fixture values
exactly; the ETF report demonstrates the explicit-unavailable path for a module whose
`mart` output is not materialized yet.

This performs no factor computation. Mapping a materialized strategy decision's
outcome/eligibility to a display availability status is a lookup over an already-computed
result, not a new metric — and it lives here on the read side, never in the builder.

Swapping in a real mart-backed port later replaces this class only; the report model, the
builder, and the renderers are untouched.
"""

from __future__ import annotations

from decimal import Decimal

from truealpha_contracts.access import AccessContext
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
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
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository

_DEFAULT_STRATEGY_ID = "large_model_value_v0"
_STRATEGY_FACTOR_VERSION = "large_model_value_v0"

# Modules whose mart output is not materialized yet (Gate 2); rendered as explicit
# unavailable sections so a company report never silently drops them.
_UNMATERIALIZED_COMPANY_SECTIONS: tuple[tuple[ReportSectionKind, str, str], ...] = (
    (ReportSectionKind.PEG_CONVENTIONS, "PEG (switchable conventions)", "peg_module_not_materialized"),
    (ReportSectionKind.SUPPLY_CHAIN, "Supply-chain scenario exposure", "supply_chain_module_not_materialized"),
    (ReportSectionKind.ANALYST_HISTORY, "Analyst track record", "analyst_module_not_materialized"),
)

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
    results = (
        _result(
            label="tier",
            value=tier_value,
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="current_price_to_sales",
            value=_decimal_str(decision.current_price_to_sales),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="target_price_to_sales",
            value=_decimal_str(decision.target_price_to_sales),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
        _result(
            label="valuation_gap",
            value=_decimal_str(decision.valuation_gap),
            decision=decision,
            section_status=status,
            source=source,
            corpus_sha256=corpus_sha256,
        ),
    )
    return ReportSection(
        section_kind=ReportSectionKind.VALUATION,
        title="Valuation (three-tier P/S)",
        availability=status,
        validation_status=FactorValidationStatus.NOT_EVALUATED,
        results=results,
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


def _unmaterialized_section(section_kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    return ReportSection(
        section_kind=section_kind,
        title=title,
        availability=AvailabilityStatus.UNAVAILABLE,
        reason_codes=(reason,),
    )


def _company_subject(decision: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSubject:
    sections = [
        _operating_efficiency_section(decision, source, corpus_sha256),
        _valuation_section(decision, source, corpus_sha256),
        *[
            _unmaterialized_section(section_kind, title, reason)
            for section_kind, title, reason in _UNMATERIALIZED_COMPANY_SECTIONS
        ],
        _strategy_summary_section(decision, source, corpus_sha256),
    ]
    return ReportSubject(
        subject_id=decision.issuer_id,
        display_name=decision.issuer_id,
        sections=tuple(sections),
    )


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
            _unmaterialized_section(ReportSectionKind.STRATEGY_SUMMARY, "Large-model-value strategy decision", reason),
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
            _unmaterialized_section(
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


class FixtureResearchReadRepository:
    """Reproduces already-materialized research sections from the strategy-run fixture.

    `strategy_repository` is typed to the shared `StrategyRunReadRepository` protocol
    (not narrowed to the fixture) so `research_report_mart.MartResearchReadRepository`
    can subclass this and inject a `PostgresStrategyRunRepository` instead, reusing this
    class's ETF/missing-subject/ranking-vs-company orchestration entirely (#369)."""

    provenance_label = "fixture:research_report.v1"

    def __init__(self, *, strategy_repository: StrategyRunReadRepository | None = None) -> None:
        self._strategy_repository = (
            strategy_repository if strategy_repository is not None else FixtureStrategyRunRepository()
        )

    def _strategy_report(self, request: ResearchReportRequest, context: AccessContext) -> StrategyRunReport | None:
        strategy_id = request.strategy_id if request.strategy_id is not None else _DEFAULT_STRATEGY_ID
        result = self._strategy_repository.get_latest(strategy_id=strategy_id, context=context)
        return result if isinstance(result, StrategyRunReport) else None

    def _decisions_at_cutoff(
        self, report: StrategyRunReport, request: ResearchReportRequest
    ) -> list[StrategyRunDecision]:
        return [decision for decision in report.decisions if decision.cutoff_at == request.cutoff_at]

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]:
        if request.report_kind is ResearchReportKind.ETF:
            return tuple(_etf_subject(entity_id, request) for entity_id in request.target_entity_ids)

        report = self._strategy_report(request, context)
        if report is None:
            return tuple(
                _missing_subject(entity_id, "strategy_run_unavailable") for entity_id in request.target_entity_ids
            )
        decisions = self._decisions_at_cutoff(report, request)

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
                subjects.append(_company_subject(decision, report.source, report.corpus_sha256))
        return tuple(subjects)
