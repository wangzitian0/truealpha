"""Mart-backed ResearchReadPort and MetricNodeValueRepository (#369, #42, #1716)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError
from truealpha_contracts.common import FrozenModel
from truealpha_contracts.confidence_bands import confidence_band
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.universes import id_prefix_like, universes_with_role

from truealpha_runtime.access import AccessContext
from truealpha_runtime.issuer_modules_mart import (
    NO_SERVED_ROW,
    IssuerModuleReader,
    PostgresIssuerModuleReader,
    load_entity_display_map,
    resolve_issuer_id_candidates_batch,
    resolve_static_entity_display,
)
from truealpha_runtime.research_report import (
    EvidenceTrace,
    ReportSection,
    ReportSectionKind,
    ReportSubject,
    ResearchReportKind,
    ResearchReportRequest,
    ResultValue,
)
from truealpha_runtime.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReadRepository,
    StrategyRunReport,
)
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository


class MetricNodeValueCell(FrozenModel):
    issuer_id: str
    node_id: str
    node_key: str
    period: str
    period_tag: str | None = None
    value: str | None
    unit: str | None = None
    availability_status: str
    reason_codes: tuple[str, ...] = ()
    source_evidence_status: str
    confidence: str | None
    confidence_band: str | None = None
    tree_version: str | None = None
    tree_sha256: str | None = None


class MetricNodeValueReport(FrozenModel):
    run_id: str
    node_key: str
    available_count: int
    cells: tuple[MetricNodeValueCell, ...]


class MetricNodeValueUnavailable(FrozenModel):
    node_key: str
    reason: str


_DEFAULT_STRATEGY_ID = _STRATEGY_FACTOR_VERSION = "large_model_value_v0"
MART_READ_FAILED, ROW_AFTER_REPORT_CUTOFF, PEG_NOT_RECORDED = (
    "mart_read_failed",
    "served_row_after_report_cutoff",
    "peg_not_recorded",
)
_ETF_DISPLAY_NAMES = {"etf:qqq": "Invesco QQQ Trust", "etf:ivv": "iShares Core S&P 500 ETF"}


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _decision_availability(d: StrategyRunDecision) -> AvailabilityStatus:
    if d.availability_status is AvailabilityStatus.NOT_APPLICABLE:
        return AvailabilityStatus.NOT_APPLICABLE
    if d.outcome is StrategyRunOutcome.EXCLUDED:
        return (
            AvailabilityStatus.LOW_CONFIDENCE
            if d.exclusion_reason == "below_confidence_floor"
            else AvailabilityStatus.EXCLUDED
        )
    return AvailabilityStatus.AVAILABLE


def _decision_section(
    d: StrategyRunDecision,
    source: str,
    corpus_sha256: str,
    kind: ReportSectionKind,
    title: str,
    labeled: tuple[tuple[str, str | None, str | None], ...],
    *,
    status_override: AvailabilityStatus | None = None,
    reason_codes: tuple[str, ...] = (),
) -> ReportSection:
    status = status_override or _decision_availability(d)
    period, cutoff_iso = d.cutoff_at.date().isoformat(), d.cutoff_at.isoformat().replace("+00:00", "Z")
    trace = EvidenceTrace(reference_id=f"{source}:{corpus_sha256[:12]}:{d.issuer_id}:{cutoff_iso}")
    return ReportSection(
        section_kind=kind,
        title=title,
        availability=status,
        results=tuple(
            ResultValue(
                label=lb,
                value=v,
                unit=u,
                period=period,
                cutoff_at=d.cutoff_at,
                availability=status
                if (v is not None or status is AvailabilityStatus.NOT_APPLICABLE)
                else AvailabilityStatus.UNAVAILABLE,
                confidence=d.confidence,
                factor_version=_STRATEGY_FACTOR_VERSION,
                trace=trace,
            )
            for lb, v, u in labeled
        ),
        reason_codes=reason_codes,
    )


def _valuation_section(d: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    by_gp = d.valuation_basis == "price_to_gross_profit"
    mid: tuple[tuple[str, str | None, str | None], ...] = (
        (
            ("current_price_to_gross_profit", _opt_str(d.current_price_to_gross_profit), None),
            ("target_price_to_gross_profit", _opt_str(d.target_price_to_gross_profit), None),
            ("current_price_to_sales", _opt_str(d.current_price_to_sales), None),
        )
        if by_gp
        else (
            ("current_price_to_sales", _opt_str(d.current_price_to_sales), None),
            ("target_price_to_sales", _opt_str(d.target_price_to_sales), None),
        )
    )
    return _decision_section(
        d,
        source,
        corpus_sha256,
        ReportSectionKind.VALUATION,
        "Valuation (three-tier P/GP)" if by_gp else "Valuation (three-tier P/S)",
        (("tier", d.tier.value if d.tier else None, None), *mid, ("valuation_gap", _opt_str(d.valuation_gap), None)),
    )


def _operating_efficiency_section(d: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    return _decision_section(
        d,
        source,
        corpus_sha256,
        ReportSectionKind.OPERATING_EFFICIENCY,
        "Operating efficiency (capital-adjusted labor efficiency)",
        (("capital_adjusted_labor_efficiency", _opt_str(d.capital_adjusted_labor_efficiency), "USD"),),
    )


def _strategy_summary_section(d: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    reasons = (
        (d.exclusion_reason,)
        if d.exclusion_reason is not None
        else (("missing_audited_gross_profit_or_coordinate_join",) if d.outcome is StrategyRunOutcome.EXCLUDED else ())
    )
    return _decision_section(
        d,
        source,
        corpus_sha256,
        ReportSectionKind.STRATEGY_SUMMARY,
        "Large-model-value strategy decision",
        (
            ("outcome", d.outcome.value, None),
            ("eligible", str(d.eligible).lower(), None),
            ("rank", _opt_str(d.rank), None),
            ("target_weight", _opt_str(d.target_weight), None),
        ),
        reason_codes=reasons,
    )


def _peg_availability(peg_present: bool, decision: AvailabilityStatus) -> AvailabilityStatus:
    """PEG has its own availability: an excluded decision can carry a PEG value (#1804)."""
    if peg_present:
        return AvailabilityStatus.AVAILABLE
    return AvailabilityStatus.UNAVAILABLE if decision is AvailabilityStatus.AVAILABLE else decision


def _peg_section(d: StrategyRunDecision, source: str, corpus_sha256: str) -> ReportSection:
    st = _decision_availability(d)
    return _decision_section(
        d,
        source,
        corpus_sha256,
        ReportSectionKind.PEG_CONVENTIONS,
        "PEG",
        (("peg", _opt_str(d.peg), None), ("peg_rank", _opt_str(d.peg_rank), None)),
        status_override=_peg_availability(d.peg is not None, st),
        reason_codes=(d.peg_reason_codes or (PEG_NOT_RECORDED,)) if d.peg is None else (),
    )


def _module_absent(kind: ReportSectionKind, title: str, reason: str) -> ReportSection:
    return ReportSection(
        section_kind=kind,
        title=title,
        availability=AvailabilityStatus.ERROR if reason == MART_READ_FAILED else AvailabilityStatus.UNAVAILABLE,
        reason_codes=(reason,),
    )


def _read_module_section(
    issuer_id: str,
    cutoff: datetime,
    kind: ReportSectionKind,
    title: str,
    table: str,
    fetch: Callable[[str], Any],
    fields: tuple[str, ...],
) -> ReportSection:
    try:
        row = fetch(issuer_id)
    except psycopg.Error:
        return _module_absent(kind, title, MART_READ_FAILED)
    if row is None:
        return _module_absent(kind, title, NO_SERVED_ROW)
    cutoff_at = datetime.fromisoformat(row.cutoff.replace("Z", "+00:00"))
    if cutoff_at > cutoff:
        return _module_absent(kind, title, ROW_AFTER_REPORT_CUTOFF)
    trace, period, conf = (
        EvidenceTrace(reference_id=f"{table}:{row.run_id}:{issuer_id}", materialized_output_id=row.run_id),
        cutoff_at.date().isoformat(),
        Decimal(row.confidence),
    )
    return ReportSection(
        section_kind=kind,
        title=title,
        availability=row.availability_status,
        reason_codes=row.reason_codes,
        results=tuple(
            ResultValue(
                label=f,
                value=_opt_str(getattr(row, f)),
                period=period,
                cutoff_at=cutoff_at,
                availability=row.availability_status if getattr(row, f) is not None else AvailabilityStatus.UNAVAILABLE,
                confidence=conf,
                factor_version=row.extractor,
                trace=trace,
            )
            for f in fields
        ),
    )


def _subject_display_name(issuer_id: str, display_map: dict[str, tuple[str, str]] | None = None) -> str:
    if display_map and (e := display_map.get(issuer_id) or display_map.get(issuer_id.upper())):
        return e[1]
    return resolve_static_entity_display(issuer_id)[2]


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
            raise TypeError("requires database_url or both strategy_repository and issuer_modules")
        self._database_url, self._strategy_repository, self._issuer_modules = (
            database_url,
            strategy_repository,
            issuer_modules,
        )

    def _load_etf_subject(self, eid: str, cutoff_at: datetime) -> ReportSubject:
        clean = eid.strip()
        token = clean.split(":")[-1].split("/")[-1].strip().upper()
        series_cand = clean if clean.lower().startswith("etf:series:") else f"etf:series:{token}"
        default_name = _ETF_DISPLAY_NAMES.get(clean.lower(), clean)
        if not self._database_url:
            return ReportSubject(
                subject_id=eid,
                display_name=default_name,
                sections=(
                    _module_absent(ReportSectionKind.ETF_VIRTUAL_COMPANY, "ETF virtual company", NO_SERVED_ROW),
                    _module_absent(ReportSectionKind.VALUATION, "Valuation (three-tier P/S)", NO_SERVED_ROW),
                ),
            )
        sql = """
          select f.run_id, f.fund_id, coalesce(nullif(f.fund_name, ''), f.fund_id) as fund_name,
                 f.cutoff, f.report_period,
                 f.weighted_valuation_gap::text as weighted_valuation_gap,
                 f.valuation_basis,
                 f.total_weight_pct::text as total_weight_pct,
                 f.resolved_weight_pct::text as resolved_weight_pct,
                 f.valued_weight_pct::text as valued_weight_pct,
                 f.lines, f.valued_lines,
                 f.confidence::text as confidence,
                 f.reason_codes,
                 coalesce(f.availability_status, 'unavailable') as availability_status,
                 f.definition_version,
                 to_jsonb(f) ->> 'virtual_revenue' as virtual_revenue,
                 to_jsonb(f) ->> 'virtual_gross_profit' as virtual_gross_profit,
                 to_jsonb(f) ->> 'virtual_employees' as virtual_employees,
                 to_jsonb(f) ->> 'virtual_gppe' as virtual_gppe
          from mart.fund_virtual_company f
          where f.run_id in (
            select h.run_id from mart.served_head h
            where h.run_id is not null and not starts_with(h.universe_id, 'universe:canary-')
          )
            and f.cutoff <= %s
            and (
              f.fund_id = %s
              or f.fund_id = %s
              or upper(f.fund_id) = %s
              or upper(f.fund_name) like %s
            )
          order by (f.fund_id = %s or f.fund_id = %s) desc, f.cutoff desc, f.created_at desc
          limit 1
        """
        pattern = f"%{token}%" if len(token) >= 2 else clean.upper()
        try:
            with psycopg.connect(self._database_url, connect_timeout=5, row_factory=dict_row) as conn:
                conn.execute("set local role mart_readonly")
                conn.execute("select set_config('statement_timeout', '5s', true)")
                row = conn.execute(sql, (cutoff_at, clean, series_cand, token, pattern, clean, series_cand)).fetchone()
                conn.execute("reset role")
        except psycopg.Error:
            return ReportSubject(
                subject_id=eid,
                display_name=default_name,
                sections=(
                    _module_absent(ReportSectionKind.ETF_VIRTUAL_COMPANY, "ETF virtual company", MART_READ_FAILED),
                    _module_absent(ReportSectionKind.VALUATION, "Valuation (three-tier P/S)", MART_READ_FAILED),
                ),
            )
        if row is None:
            return ReportSubject(
                subject_id=eid,
                display_name=default_name,
                sections=(
                    _module_absent(ReportSectionKind.ETF_VIRTUAL_COMPANY, "ETF virtual company", NO_SERVED_ROW),
                    _module_absent(ReportSectionKind.VALUATION, "Valuation (three-tier P/S)", NO_SERVED_ROW),
                ),
            )
        row_cutoff = row["cutoff"]
        period = (
            row["report_period"].isoformat() if row.get("report_period") is not None else row_cutoff.date().isoformat()
        )
        avail = AvailabilityStatus(str(row["availability_status"]))
        conf = Decimal(str(row["confidence"])) if row.get("confidence") is not None else None
        reasons = tuple(str(c) for c in (row.get("reason_codes") or ()))
        fver = str(row.get("definition_version") or "etf_virtual_company:v0")
        trace = EvidenceTrace(
            reference_id=f"mart.fund_virtual_company:{row['run_id']}:{row['fund_id']}",
            materialized_output_id=str(row["run_id"]),
        )

        def _rv(label: str, value: Any, unit: str | None = None) -> ResultValue:
            txt = None if value is None else str(value)
            return ResultValue(
                label=label,
                value=txt,
                unit=unit,
                period=period,
                cutoff_at=row_cutoff,
                availability=avail if txt is not None else AvailabilityStatus.UNAVAILABLE,
                confidence=conf,
                factor_version=fver,
                trace=trace,
            )

        return ReportSubject(
            subject_id=eid,
            display_name=str(row.get("fund_name") or default_name),
            sections=(
                ReportSection(
                    section_kind=ReportSectionKind.ETF_VIRTUAL_COMPANY,
                    title="ETF virtual company",
                    availability=avail,
                    reason_codes=reasons,
                    results=(
                        _rv("weighted_valuation_gap", row.get("weighted_valuation_gap")),
                        _rv("valued_weight_pct", row.get("valued_weight_pct")),
                        _rv("resolved_weight_pct", row.get("resolved_weight_pct")),
                        _rv("virtual_revenue", row.get("virtual_revenue"), "USD"),
                        _rv("virtual_gross_profit", row.get("virtual_gross_profit"), "USD"),
                        _rv("virtual_employees", row.get("virtual_employees")),
                        _rv("virtual_gppe", row.get("virtual_gppe"), "USD"),
                    ),
                ),
                ReportSection(
                    section_kind=ReportSectionKind.VALUATION,
                    title="Valuation (ETF consolidation)",
                    availability=avail,
                    reason_codes=reasons,
                    results=(
                        _rv("valuation_basis", row.get("valuation_basis")),
                        _rv("weighted_valuation_gap", row.get("weighted_valuation_gap")),
                        _rv("valued_lines", row.get("valued_lines")),
                        _rv("lines", row.get("lines")),
                    ),
                ),
            ),
        )

    def load_subjects(self, *, request: ResearchReportRequest, context: AccessContext) -> tuple[ReportSubject, ...]:
        if request.report_kind is ResearchReportKind.ETF:
            return tuple(self._load_etf_subject(eid, request.cutoff_at) for eid in request.target_entity_ids)
        res = self._strategy_repository.get_latest(
            strategy_id=request.strategy_id or _DEFAULT_STRATEGY_ID, context=context
        )
        report = res if isinstance(res, StrategyRunReport) else None
        if report is None:
            return tuple(
                ReportSubject(
                    subject_id=eid,
                    display_name=eid,
                    sections=(
                        _module_absent(
                            ReportSectionKind.STRATEGY_SUMMARY,
                            "Large-model-value strategy decision",
                            "strategy_run_unavailable",
                        ),
                    ),
                )
                for eid in request.target_entity_ids
            )
        decisions = [d for d in report.decisions if d.cutoff_at == request.cutoff_at]
        display_map = load_entity_display_map(self._database_url) if self._database_url is not None else None
        by_issuer: dict[str, StrategyRunDecision] = {}
        for d in decisions:
            by_issuer[d.issuer_id] = d
            by_issuer.setdefault(d.issuer_id.upper(), d)
            if t := resolve_static_entity_display(d.issuer_id)[1]:
                by_issuer.setdefault(t.upper(), d)
        unmatched = [
            e for e in request.target_entity_ids if by_issuer.get(e) is None and by_issuer.get(e.upper()) is None
        ]
        resolved = (
            resolve_issuer_id_candidates_batch(self._database_url, unmatched)
            if unmatched and self._database_url
            else {}
        )

        def _match(eid: str) -> StrategyRunDecision | None:
            return (
                by_issuer.get(eid)
                or by_issuer.get(eid.upper())
                or next(
                    (m for c in resolved.get(eid.strip(), ()) if (m := by_issuer.get(c) or by_issuer.get(c.upper()))),
                    None,
                )
            )

        if request.report_kind is ResearchReportKind.THEME_RANKING:
            ordered = sorted(
                decisions, key=lambda d: (1, 0, d.issuer_id) if d.rank is None else (0, d.rank, d.issuer_id)
            )
            targets = [e for e in request.target_entity_ids if e.strip() and not e.strip().lower().startswith("theme:")]
            if targets and (allowed := {m.issuer_id for t in targets if (m := _match(t)) is not None}):
                ordered = [d for d in ordered if d.issuer_id in allowed]
            if not ordered:
                return tuple(
                    ReportSubject(
                        subject_id=eid,
                        display_name=eid,
                        sections=(
                            _module_absent(
                                ReportSectionKind.STRATEGY_SUMMARY,
                                "Large-model-value strategy decision",
                                "strategy_run_unavailable",
                            ),
                        ),
                    )
                    for eid in request.target_entity_ids
                )
            return tuple(
                ReportSubject(
                    subject_id=d.issuer_id,
                    display_name=_subject_display_name(d.issuer_id, display_map),
                    rank=d.rank,
                    sections=(
                        _strategy_summary_section(d, report.source, report.corpus_sha256),
                        _valuation_section(d, report.source, report.corpus_sha256),
                    ),
                )
                for d in ordered
            )

        out: list[ReportSubject] = []
        for eid in request.target_entity_ids:
            matched = _match(eid)
            if matched is None:
                out.append(
                    ReportSubject(
                        subject_id=eid,
                        display_name=eid,
                        sections=(
                            _module_absent(
                                ReportSectionKind.STRATEGY_SUMMARY,
                                "Large-model-value strategy decision",
                                "subject_not_in_strategy_run",
                            ),
                        ),
                    )
                )
                continue
            out.append(
                ReportSubject(
                    subject_id=matched.issuer_id,
                    display_name=_subject_display_name(matched.issuer_id, display_map),
                    sections=(
                        _operating_efficiency_section(matched, report.source, report.corpus_sha256),
                        _valuation_section(matched, report.source, report.corpus_sha256),
                        _peg_section(matched, report.source, report.corpus_sha256),
                        _read_module_section(
                            matched.issuer_id,
                            matched.cutoff_at,
                            ReportSectionKind.SUPPLY_CHAIN,
                            "Supply-chain exposure",
                            "mart.issuer_supply_chain_exposure",
                            self._issuer_modules.supply_chain_exposure,
                            (
                                "exposure_score",
                                "direct_partners",
                                "suppliers_count",
                                "customers_count",
                                "max_partner_share",
                            ),
                        ),
                        _read_module_section(
                            matched.issuer_id,
                            matched.cutoff_at,
                            ReportSectionKind.ANALYST_HISTORY,
                            "Analyst consensus",
                            "mart.issuer_analyst_ratings",
                            self._issuer_modules.analyst_ratings,
                            (
                                "consensus_rating",
                                "target_price",
                                "analysts_count",
                                "buy_count",
                                "hold_count",
                                "sell_count",
                            ),
                        ),
                        _strategy_summary_section(matched, report.source, report.corpus_sha256),
                    ),
                )
            )
        return tuple(out)


POINTER_HEAD_SQL = """
  select run_id, freshness, availability, staleness_reason
  from mart.served_head
  where factor_id = 'gross_profit_per_employee'
    and universe_id like %s
  order by advanced_at desc limit 1
"""


def resolve_strategy_governed_run(conn: Any) -> tuple[str | None, str | None]:
    prefixes = [(id_prefix_like(k),) for k in universes_with_role("strategy")]
    unavailable_reason: str | None = None
    for like_prefix in prefixes:
        head = conn.execute(POINTER_HEAD_SQL, like_prefix).fetchone()
        if head is not None:
            if head["availability"] == "unavailable" or head["run_id"] is None:
                unavailable_reason = (
                    unavailable_reason or f"head_unavailable: {head['staleness_reason'] or 'older_than_30d'}"
                )
                continue
            return str(head["run_id"]), None
    if unavailable_reason is not None:
        return None, unavailable_reason
    return None, "no served head for strategy"


class PostgresMetricNodeValueRepository:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def latest(
        self, *, node_key: str, issuer_id: str | None = None, limit: int = 100
    ) -> MetricNodeValueReport | MetricNodeValueUnavailable:
        if not node_key or not node_key.strip() or not 1 <= limit <= 500:
            raise ValueError("invalid node_key or limit")
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                conn.execute("set local role mart_readonly")
                conn.execute("select set_config('statement_timeout', '5s', true)")
                run_id, err = resolve_strategy_governed_run(conn)
                if err is not None or run_id is None:
                    conn.execute("reset role")
                    return MetricNodeValueUnavailable(node_key=node_key, reason=err or "no_run")
                where = "v.run_id = %s and v.node_key = %s" + (" and v.issuer_id = %s" if issuer_id is not None else "")
                rows = conn.execute(
                    f"select v.issuer_id, v.node_id::text as node_id, v.node_key, v.period, v.period_tag, v.value::text as value, v.unit, v.availability_status, v.reason_codes, v.source_evidence_status, v.confidence::text as confidence, v.tree_version, v.tree_sha256 from mart.metric_node_values v where {where} order by v.issuer_id, v.period desc limit %s",
                    (run_id, node_key, *((issuer_id,) if issuer_id is not None else ()), limit),
                ).fetchall()
                conn.execute("reset role")
                cells = tuple(
                    MetricNodeValueCell(
                        **(
                            r
                            | {
                                "reason_codes": tuple(r["reason_codes"] or ()),
                                "confidence_band": confidence_band(
                                    None if r["confidence"] is None else Decimal(r["confidence"])
                                ),
                            }
                        )
                    )
                    for r in rows
                )
                return MetricNodeValueReport(
                    run_id=run_id,
                    node_key=node_key,
                    available_count=sum(c.availability_status == "available" for c in cells),
                    cells=cells,
                )
        except psycopg.Error:
            return MetricNodeValueUnavailable(node_key=node_key, reason="database_unavailable")
        except (KeyError, ValueError, TypeError, ValidationError):
            return MetricNodeValueUnavailable(node_key=node_key, reason="schema_mismatch")
