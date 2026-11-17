"""MCP endpoint registering read-only mart tools behind BearerTokenGate (#348, #758, #1104, #42)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.issuer_modules_mart import (
    AnalystRatings,
    IssuerModuleReader,
    IssuerModuleUnavailable,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
    issuer_identity_sql,
    resolve_static_entity_display,
)
from truealpha_contracts.research_cards import (
    CardKind,
    ResearchCard,
    build_card,
    build_strategy_context_from_report,
    enrich_card_metadata,
    paginate_card_series,
    render_card_html,
)
from truealpha_contracts.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
    render_report_markdown,
)
from truealpha_contracts.research_report_mart import MartResearchReadRepository
from truealpha_contracts.strategy_run import StrategyRunReadRepository, StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository
from truealpha_contracts.topt_read import (
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
    PostgresToptGppeRepository,
    ToptGppeReport,
    ToptGppeUnavailable,
)

from llm_service.config import settings

logger = logging.getLogger(__name__)

_OWNER_PRINCIPAL_ID = "principal:owner"
_CONTEXT_LIFETIME = timedelta(minutes=5)
MART_READ_FAILED_MESSAGE = "the mart read failed; the detail is in the service log"
_LEGACY_JOIN = (
    "left join lateral ("
    "select ei.legacy_id from mart.entity_identity ei where ei.entity_id::text = {col} limit 1"
    ") ei on true"
)


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StrategyRunToolRequest(_StrictToolModel):
    strategy_id: str


class IssuerModuleToolRequest(_StrictToolModel):
    issuer_id: str


class ResearchReportToolRequest(_ToolModel):
    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None


class ResearchReportMarkdown(_ToolModel):
    report_id: str
    schema_version: str
    markdown: str


class ResearchCardToolRequest(ResearchReportToolRequest):
    card_kind: CardKind
    style_preset: str | None = None
    page: int | None = None


class CompanyProfileToolRequest(_ToolModel):
    issuer_id: str


class ThemePurityToolRequest(_ToolModel):
    theme_id: str
    limit: int = 10


class EtfProfileToolRequest(_ToolModel):
    fund_id: str = "etf:series:S000101292"


class BacktestToolRequest(_ToolModel):
    run_id: str | None = None
    strategy_key: str | None = None
    limit: int = 10


class MetricNodeValueToolRequest(_ToolModel):
    node_key: str
    issuer_id: str | None = None
    limit: int = 100


def _owner_access_context() -> AccessContext:
    now = datetime.now(UTC)
    stamp = now.timestamp()
    return AccessContext(
        context_id=f"ctx:mcp-owner:{stamp}",
        principal_id=_OWNER_PRINCIPAL_ID,
        tenant_id="tenant:truealpha",
        session_id=f"session:mcp-owner:{stamp}",
        authentication_method=AuthenticationMethod.DELEGATED_MCP,
        principal_kind=PrincipalKind.ADMINISTRATOR,
        issued_at=now,
        expires_at=now + _CONTEXT_LIFETIME,
        delegated_by_service_principal_id="principal:llm-service-mcp",
        delegation_id="delegation:mcp-bearer-token",
    )


class _AuditedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        started = datetime.now(UTC)
        outcome = "error"
        try:
            result = await super().call_tool(name, arguments)
            outcome = "ok"
            return result
        finally:
            elapsed_ms = round((datetime.now(UTC) - started).total_seconds() * 1000)
            logger.info(
                "mcp tool call: tool=%s principal=%s outcome=%s duration_ms=%d",
                name,
                _OWNER_PRINCIPAL_ID,
                outcome,
                elapsed_ms,
            )


class CompanyProfileReader(Protocol):
    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]: ...


class ThemePurityLeaderboardReader(Protocol):
    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]: ...


class EtfProfileReader(Protocol):
    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]: ...


class BacktestReader(Protocol):
    def get_backtest_report(
        self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
    ) -> dict[str, Any]: ...


def _opt_str(row: Mapping[str, Any] | None, key: str) -> str | None:
    return str(v) if row and (v := row.get(key)) is not None else None


def _opt_int(row: Mapping[str, Any] | None, key: str) -> int | None:
    return int(v) if row and (v := row.get(key)) is not None else None


def _opt_list(row: Mapping[str, Any] | None, key: str) -> list[Any]:
    return list(v) if row and (v := row.get(key)) is not None else []


def _pick_str(row: Mapping[str, Any] | None, *keys: str) -> dict[str, str | None]:
    return {k: _opt_str(row, k) for k in keys}


def _text_cols(*cols: str) -> str:
    return ", ".join(f"{c}::text as {c.split('.')[-1]}" for c in cols)


class _PgReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def _query(
        self,
        label: str,
        fn: Callable[[psycopg.Cursor[dict[str, Any]]], Any],
        default: Any,
        *,
        on_error: Callable[[Exception], Any] | None = None,
    ) -> Any:
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn, conn.cursor() as cur:
                return fn(cur)
        except Exception as exc:
            logger.warning("%s failed to query database: %s", label, exc)
            if on_error is not None:
                return on_error(exc)
            if isinstance(exc, psycopg.Error):
                return default
            raise


def _format_theme_row(row: Mapping[str, Any], *, include_issuer: bool = False) -> dict[str, Any]:
    seg = _opt_int(row, "segments")
    out: dict[str, Any] = {
        "theme_id": str(row.get("theme_id", "")),
        "theme": str(row.get("theme", "")),
        **_pick_str(row, "theme_share", "in_theme_revenue", "consolidated_revenue", "confidence"),
        "segments": (seg or 0) if include_issuer else seg,
        "availability_status": str(row.get("availability_status", "unavailable")),
    }
    return {"issuer_id": str(row["issuer_id"]), "cik": _opt_int(row, "cik"), **out} if include_issuer else out


_UTC_ISO = 'to_char({col}, \'YYYY-MM-DD"T"HH24:MI:SS"Z"\')'
_DECISION_SQL = (
    f"select coalesce(ei.legacy_id, d.issuer_id) as issuer_id, {_UTC_ISO.format(col='d.cutoff_at')} as cutoff_at, d.tier, "
    + _text_cols(
        "d.valuation_gap",
        "d.current_price_to_sales",
        "d.target_price_to_sales",
        "d.current_price_to_gross_profit",
        "d.target_price_to_gross_profit",
        "d.peg",
        "t.confidence",
    )
    + ", d.outcome, d.exclusion_reason, d.rank, d.peg_rank, d.peg_reason_codes, t.reason_codes"
    " from mart.strategy_decisions d"
    " left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id"
    " left join mart.topt_core_results t on t.run_id = scope.capture_run_id and t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at"
    f" {_LEGACY_JOIN.format(col='d.issuer_id')} where {{where}} order by d.cutoff_at desc limit 1"
)
_GPPE_SQL = (
    f"select {_text_cols('gppe', 'capital_adjusted_gross_profit', 'confidence')}, operating_branch,"
    " coalesce(availability_status, availability, 'unavailable') as availability_status,"
    " reason_codes from mart.topt_gppe_results g where {where} order by g.cutoff desc, g.created_at desc limit 1"
)
_ISSUER_THEME_SQL = (
    "select theme_id, theme, theme_share, in_theme_revenue, consolidated_revenue, segments, confidence, availability_status"
    " from (select distinct on (p.theme_id) p.theme_id, p.theme, p.theme_share as raw_share, p.segments, "
    + _text_cols("p.theme_share", "p.in_theme_revenue", "p.consolidated_revenue", "p.confidence")
    + ", coalesce(p.availability_status, 'unavailable') as availability_status"
    " from mart.issuer_theme_purity p where {where} and p.partition_id is not null"
    " order by p.theme_id, p.cutoff desc, p.created_at desc) latest_per_theme"
    " order by raw_share desc nulls last, theme asc"
)


class PostgresCompanyProfileReader(_PgReader):
    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]:
        clean = issuer_id.strip()
        params = (clean, clean, clean)

        def _load(cur: psycopg.Cursor[dict[str, Any]]) -> tuple[Any, Any, list[dict[str, Any]]]:
            rows: list[Any] = []
            for sql_tpl, alias, fetch_all in (
                (_DECISION_SQL, "d.issuer_id", False),
                (_GPPE_SQL, "g.issuer_id", False),
                (_ISSUER_THEME_SQL, "p.issuer_id", True),
            ):
                cur.execute(sql_tpl.format(where=issuer_identity_sql(alias, "%s")), params)
                rows.append([_format_theme_row(r) for r in cur.fetchall()] if fetch_all else cur.fetchone())
            return rows[0], rows[1], rows[2]

        d_row, g_row, themes = self._query("get_company_profile", _load, (None, None, []))
        return _format_company_profile(issuer_id=issuer_id, decision_row=d_row, gppe_row=g_row, themes=themes)


def _decision_reason_codes(row: Mapping[str, Any] | None) -> list[str]:
    if not row:
        return []
    if codes := row.get("reason_codes"):
        return [str(c) for c in codes]
    if exclusion := _opt_str(row, "exclusion_reason"):
        return [exclusion]
    return ["missing_audited_gross_profit_or_coordinate_join"] if _opt_str(row, "outcome") == "excluded" else []


def _format_company_profile(
    *,
    issuer_id: str,
    decision_row: Mapping[str, Any] | None,
    gppe_row: Mapping[str, Any] | None,
    themes: list[dict[str, Any]],
) -> dict[str, Any]:
    target_pgp = _opt_str(decision_row, "target_price_to_gross_profit")
    target_ps = _opt_str(decision_row, "target_price_to_sales")
    basis = "price_to_gross_profit" if target_pgp is not None else ("price_to_sales" if target_ps is not None else None)
    resolved_id = _opt_str(decision_row, "issuer_id") or issuer_id
    gppe_conf, gppe_codes = _opt_str(gppe_row, "confidence"), _opt_list(gppe_row, "reason_codes")
    decision_codes = _decision_reason_codes(decision_row)

    _, ticker, display_name = resolve_static_entity_display(resolved_id)
    if ticker is None and issuer_id != resolved_id:
        _, ticker, display_name = resolve_static_entity_display(issuer_id)

    shared = {
        **_pick_str(
            decision_row,
            "tier",
            "valuation_gap",
            "current_price_to_sales",
            "target_price_to_sales",
            "current_price_to_gross_profit",
            "target_price_to_gross_profit",
            "peg",
        ),
        "valuation_basis": basis,
        "confidence": _opt_str(decision_row, "confidence") or gppe_conf,
        "peg_rank": _opt_int(decision_row, "peg_rank"),
        "peg_reason_codes": _opt_list(decision_row, "peg_reason_codes"),
    }
    gppe_val = _opt_str(gppe_row, "gppe")
    return {
        "issuer_id": resolved_id,
        "ticker": ticker,
        "display_name": display_name,
        **shared,
        "reason_codes": decision_codes if decision_row else gppe_codes,
        "gppe": gppe_val,
        "gppe_detail": (
            {
                "gppe": gppe_val,
                **_pick_str(gppe_row, "operating_branch", "capital_adjusted_gross_profit"),
                "confidence": gppe_conf,
                "availability_status": _opt_str(gppe_row, "availability_status") or "unavailable",
                "reason_codes": gppe_codes,
            }
            if gppe_row
            else None
        ),
        "strategy_decision": (
            {**_pick_str(decision_row, "cutoff_at", "outcome"), **shared, "reason_codes": decision_codes}
            if decision_row
            else None
        ),
        "theme_purity": themes,
        "availability_status": "available" if (decision_row or gppe_row or themes) else "unavailable",
    }


def normalize_theme_slug(theme_id: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[_\s]+", "-", theme_id.strip().lower()))


def resolve_fund_resolution_candidates(fund_id: str) -> dict[str, Any]:
    cid = fund_id.strip()
    tok = cid.split(":")[-1].split("/")[-1].strip()
    u_tok, l_tok = tok.upper(), tok.lower()
    return {
        "clean_id": cid,
        "series_candidate": cid if cid.lower().startswith("etf:series:") else f"etf:series:{u_tok}",
        "upper_token": u_tok,
        "clean_upper": cid.upper(),
        "name_pattern": f"%{l_tok}%" if len(l_tok) >= 2 else "",
    }


_LEADERBOARD_SQL = (
    "with latest_per_issuer as ("
    " select distinct on (p.issuer_id) p.issuer_id, p.cik, p.theme_id, p.theme, p.theme_share, p.in_theme_revenue,"
    "  p.consolidated_revenue, p.segments, p.confidence, coalesce(p.availability_status, 'unavailable') as availability_status,"
    "  p.cutoff, p.created_at from mart.issuer_theme_purity p"
    " where (p.theme_id = %s or p.theme_id = %s or lower(p.theme) = lower(%s)) and p.partition_id is not null"
    " order by p.issuer_id, p.cutoff desc, p.created_at desc)"
    " select coalesce(ei.legacy_id, l.issuer_id) as issuer_id, l.cik, l.theme_id, l.theme, l.segments, l.availability_status, "
    + _text_cols("l.theme_share", "l.in_theme_revenue", "l.consolidated_revenue", "l.confidence")
    + f" from latest_per_issuer l {_LEGACY_JOIN.format(col='l.issuer_id')}"
    " order by (l.theme_share) desc nulls last, (l.confidence) desc nulls last limit %s"
)


class PostgresThemePurityLeaderboardReader(_PgReader):
    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]:
        clamped = max(1, min(limit, 100))

        def _load(cur: psycopg.Cursor[dict[str, Any]]) -> list[dict[str, Any]]:
            cur.execute(_LEADERBOARD_SQL, (theme_id, normalize_theme_slug(theme_id), theme_id.strip(), clamped))
            return [_format_theme_row(r, include_issuer=True) for r in cur.fetchall()]

        issuers = self._query("get_leaderboard", _load, [])
        return {"theme_id": theme_id, "limit": clamped, "count": len(issuers), "issuers": issuers}


def _etf_match_clauses(prefix: str, id_param: str) -> tuple[str, str]:
    col_id, col_name = f"{prefix}fund_id", f"{prefix}fund_name"
    id_sub = (
        "select entity_id::text from mart.entity_identity "
        "where upper(current_ticker) in (%(upper_token)s, %(clean_upper)s) "
        "or upper(legacy_id) in (%(upper_token)s, %(clean_upper)s)"
    )
    where = (
        f"{col_id} = %({id_param})s or {col_id} = %(series_candidate)s or {col_id} in ({id_sub}) "
        f"or (%(name_pattern)s <> '' and (lower({col_name}) like %(name_pattern)s or lower({col_id}) like %(name_pattern)s))"
    )
    rank = (
        f"case when {col_id} = %({id_param})s or {col_id} = %(series_candidate)s then 1 "
        f"when {col_id} in ({id_sub}) then 2 else 3 end asc"
    )
    return where, rank


_FUND_SQL_TEMPLATE = (
    f"select fund_id, coalesce(fund_name, fund_id) as fund_name, {_UTC_ISO.format(col='cutoff')} as cutoff,"
    " to_char(report_period, 'YYYY-MM-DD') as report_period,"
    " valuation_basis, lines, valued_lines, coalesce(availability_status, 'unavailable') as availability_status, reason_codes, "
    + _text_cols("weighted_valuation_gap", "total_weight_pct", "resolved_weight_pct", "valued_weight_pct", "confidence")
    + " from mart.fund_virtual_company where {where} order by {rank}, cutoff desc, created_at desc limit 1"
)
_HOLDINGS_SQL_TEMPLATE = (
    "select coalesce(fund_name, fund_id) as fund_name, to_char(report_period, 'YYYY-MM-DD') as report_period,"
    " holding_name, ticker, isin, percent_of_net_assets::text as weight_pct, value_usd::text as value_usd,"
    " coalesce(ei.legacy_id, v.issuer_entity) as issuer_entity, v.listing_id"
    f" from mart.fund_holdings_valuation v {_LEGACY_JOIN.format(col='v.issuer_entity')}"
    " where {where} order by {rank}, v.percent_of_net_assets desc nulls last, v.holding_name asc limit 100"
)
_HOLDING_KEYS = ("ticker", "isin", "weight_pct", "value_usd", "issuer_entity", "listing_id")
_WEIGHT_DEFAULTS = ("total_weight_pct", "resolved_weight_pct", "valued_weight_pct")


class PostgresEtfProfileReader(_PgReader):
    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]:
        params = resolve_fund_resolution_candidates(fund_id)
        f_where, f_rank = _etf_match_clauses("", "clean_id")
        h_where, h_rank = _etf_match_clauses("v.", "target_fund_id")

        def _load(cur: psycopg.Cursor[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
            cur.execute(_FUND_SQL_TEMPLATE.format(where=f_where, rank=f_rank), params)
            fund_row = cur.fetchone()
            tid = str(fund_row["fund_id"]) if fund_row else params["clean_id"]
            cur.execute(_HOLDINGS_SQL_TEMPLATE.format(where=h_where, rank=h_rank), {**params, "target_fund_id": tid})
            return fund_row, cur.fetchall()

        f_row, raw_h = self._query("get_etf_profile", _load, (None, []))
        first = raw_h[0] if raw_h else None
        holdings = [{"holding_name": str(r.get("holding_name", "")), **_pick_str(r, *_HOLDING_KEYS)} for r in raw_h]
        lines, codes = _opt_int(f_row, "lines"), f_row.get("reason_codes") if f_row else None
        return {
            "fund_id": fund_id,
            "fund_name": _opt_str(f_row, "fund_name") or _opt_str(first, "fund_name") or fund_id,
            "report_period": _opt_str(f_row, "report_period") or _opt_str(first, "report_period"),
            **_pick_str(f_row, "cutoff", "weighted_valuation_gap", "confidence"),
            "weighted_valuation_gap_basis": _opt_str(f_row, "valuation_basis"),
            **{k: _opt_str(f_row, k) or "0.00" for k in _WEIGHT_DEFAULTS},
            "lines": lines if lines is not None else len(holdings),
            "valued_lines": _opt_int(f_row, "valued_lines") or 0,
            "availability_status": _opt_str(f_row, "availability_status")
            or ("available" if holdings else "unavailable"),
            "reason_codes": list(codes)
            if codes is not None
            else ([] if holdings else ["no_virtual_company_consolidation_found"]),
            "holdings": holdings,
        }


def backtest_run_limitations(run: dict[str, Any]) -> list[str]:
    payload = run.get("metrics_payload")
    stored = payload.get("limitations") if isinstance(payload, dict) else None
    return [str(item) for item in stored] if isinstance(stored, list) else []


_BACKTEST_RUN_SELECT = (
    "select run_id, strategy_key, strategy_version, universe_id, status, metrics_payload, error_message,"
    " definition_content_sha256, price_basis, claim_ceiling, benchmark_rule, "
    + _text_cols(
        "start_date",
        "end_date",
        "executed_at",
        "created_at",
        "cagr_monthly",
        "sharpe_daily",
        "max_dd_daily",
        "vol_daily",
        "turnover_monthly",
        "calmar_daily",
        "benchmark_cagr",
        "benchmark_max_dd",
    )
    + " from mart.backtest_runs"
)
_BACKTEST_VALUATIONS_SQL = (
    "select run_id, resolution, valuation_date::text, "
    + _text_cols("cum_nav", "drawdown", "gross_exposure", "cash_weight", "benchmark_cum_nav")
    + " from mart.backtest_valuations where run_id = %s order by valuation_date desc limit 100"
)
_BACKTEST_TRADES_SQL = (
    "select trade_id, run_id, trade_date::text, symbol, side, "
    + _text_cols("shares", "execution_price", "trade_value", "weight_before", "weight_after", "fee_paid")
    + " from mart.backtest_trades where run_id = %s order by trade_date desc limit 100"
)


class PostgresBacktestReader(_PgReader):
    def get_backtest_report(
        self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        def _load(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any]:
            vals: list[dict[str, Any]] = []
            trades: list[dict[str, Any]] = []
            if run_id:
                cur.execute(f"{_BACKTEST_RUN_SELECT} where run_id = %s limit 1", (run_id,))
                run_row = cur.fetchone()
                runs = [run_row] if run_row else []
                if run_row:
                    cur.execute(_BACKTEST_VALUATIONS_SQL, (run_id,))
                    vals = cur.fetchall()
                    cur.execute(_BACKTEST_TRADES_SQL, (run_id,))
                    trades = cur.fetchall()
            else:
                where = " where strategy_key = %s" if strategy_key else ""
                cur.execute(
                    f"{_BACKTEST_RUN_SELECT}{where} order by created_at desc limit %s",
                    (strategy_key, limit) if strategy_key else (limit,),
                )
                runs = cur.fetchall()
            for r in runs:
                r["limitations"] = backtest_run_limitations(r)
            return {
                "runs": runs,
                "valuations": vals,
                "trades": trades,
                "count": len(runs),
                "availability_status": "available" if runs else "empty",
            }

        return self._query(
            "get_backtest_report",
            _load,
            None,
            on_error=lambda exc: {
                "runs": [],
                "valuations": [],
                "trades": [],
                "count": 0,
                "availability_status": "unavailable",
                "error": str(exc),
            },
        )


def _pg_default[ReaderT](cls: Callable[..., ReaderT]) -> Callable[[], ReaderT]:
    return lambda: cls(database_url=settings.database_url)


_default_repository = _pg_default(PostgresStrategyRunRepository)
_default_research_report_repository = _pg_default(MartResearchReadRepository)
_default_issuer_module_reader = _pg_default(PostgresIssuerModuleReader)
_default_company_profile_reader = _pg_default(PostgresCompanyProfileReader)
_default_theme_purity_reader = _pg_default(PostgresThemePurityLeaderboardReader)
_default_etf_profile_reader = _pg_default(PostgresEtfProfileReader)
_default_backtest_reader = _pg_default(PostgresBacktestReader)


def _to_report_request(request: ResearchReportToolRequest, *, for_card: bool = False) -> ResearchReportRequest:
    rk, sk = request.report_kind, request.section_kinds
    if for_card and isinstance(request, ResearchCardToolRequest):
        if (
            request.card_kind is CardKind.RANKING
            and rk is ResearchReportKind.COMPANY
            and all(t.strip().lower().startswith("theme:") for t in request.target_entity_ids if t.strip())
        ):
            rk = ResearchReportKind.THEME_RANKING
        if sk == (ReportSectionKind.RANKING,):
            sk = ()
    return ResearchReportRequest(
        report_kind=rk,
        target_entity_ids=request.target_entity_ids,
        cutoff_at=request.cutoff_at,
        section_kinds=sk,
        strategy_id=request.strategy_id,
        title=request.title,
    )


def _handle_issuer_module(
    read_fn: Callable[[str], SupplyChainExposure | AnalystRatings | None],
    expected_type: type[SupplyChainExposure | AnalystRatings],
    module_name: Literal["supply_chain_exposure", "analyst_ratings"],
    issuer_id: str,
    tool_name: str,
) -> Any:
    try:
        row = read_fn(issuer_id)
    except psycopg.Error:
        logger.exception("%s: the mart read failed", tool_name)
        raise ToolError(MART_READ_FAILED_MESSAGE) from None
    return row if isinstance(row, expected_type) else IssuerModuleUnavailable(issuer_id=issuer_id, module=module_name)


@dataclass(slots=True)
class _McpTools:
    repo: StrategyRunReadRepository
    topt: PostgresToptGppeRepository
    metric_node: PostgresMetricNodeValueRepository
    research: ResearchReadPort
    company: CompanyProfileReader
    theme: ThemePurityLeaderboardReader
    etf: EtfProfileReader
    backtest: BacktestReader
    modules: IssuerModuleReader

    def _build_report(self, request: ResearchReportToolRequest, *, for_card: bool = False) -> ResearchReport:
        return build_research_report(
            _to_report_request(request, for_card=for_card), self.research, context=_owner_access_context()
        )

    def strategy_run(self, request: StrategyRunToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return self.repo.get_latest(strategy_id=request.strategy_id, context=_owner_access_context())

    def topt_gppe(self) -> ToptGppeReport | ToptGppeUnavailable:
        return self.topt.latest()

    def metric_node_values(
        self, request: MetricNodeValueToolRequest
    ) -> MetricNodeValueReport | MetricNodeValueUnavailable:
        return self.metric_node.latest(node_key=request.node_key, issuer_id=request.issuer_id, limit=request.limit)

    def research_report(self, request: ResearchReportToolRequest) -> ResearchReport:
        return self._build_report(request)

    def research_report_markdown(self, request: ResearchReportToolRequest) -> ResearchReportMarkdown:
        rep = self._build_report(request)
        return ResearchReportMarkdown(
            report_id=rep.report_id, schema_version=rep.schema_version, markdown=render_report_markdown(rep)
        )

    def research_card(self, request: ResearchCardToolRequest) -> ResearchCard:
        rep = self._build_report(request, for_card=True)
        card = build_card(rep, request.card_kind, title=request.title)
        is_mart = getattr(self.research, "_database_url", None) is not None
        strategy_ctx = (
            build_strategy_context_from_report(rep, strategy_id=request.strategy_id or "large_model_value_v0")
            if is_mart and request.card_kind in (CardKind.RANKING, CardKind.COMPARISON, CardKind.STRATEGY_SUMMARY)
            else None
        )
        card = enrich_card_metadata(
            card,
            rep,
            strategy_context=strategy_ctx,
            style_preset=request.style_preset,
            include_rendered_html=False,
        )
        if request.page is not None:
            pages = paginate_card_series(card, preset=request.style_preset)
            card = pages[max(0, min(request.page - 1, len(pages) - 1))]
        return card.model_copy(update={"rendered_html": render_card_html(card, preset=request.style_preset)})

    def company_360_profile(self, request: CompanyProfileToolRequest) -> dict[str, Any]:
        return self.company.get_company_profile(issuer_id=request.issuer_id)

    def theme_purity_leaderboard(self, request: ThemePurityToolRequest) -> dict[str, Any]:
        return self.theme.get_leaderboard(theme_id=request.theme_id, limit=request.limit)

    def etf_virtual_company_profile(self, request: EtfProfileToolRequest) -> dict[str, Any]:
        return self.etf.get_etf_profile(fund_id=request.fund_id)

    def issuer_supply_chain_exposure(
        self, request: IssuerModuleToolRequest
    ) -> SupplyChainExposure | IssuerModuleUnavailable:
        return _handle_issuer_module(
            self.modules.supply_chain_exposure,
            SupplyChainExposure,
            "supply_chain_exposure",
            request.issuer_id,
            "issuer_supply_chain_exposure",
        )

    def issuer_analyst_ratings(self, request: IssuerModuleToolRequest) -> AnalystRatings | IssuerModuleUnavailable:
        return _handle_issuer_module(
            self.modules.analyst_ratings,
            AnalystRatings,
            "analyst_ratings",
            request.issuer_id,
            "issuer_analyst_ratings",
        )

    def governed_backtest(self, request: BacktestToolRequest) -> dict[str, Any]:
        return self.backtest.get_backtest_report(
            run_id=request.run_id, strategy_key=request.strategy_key, limit=request.limit
        )


_TOOL_DESCRIPTIONS: Mapping[str, str] = {
    "strategy_run": "Read the latest large_model_value_v0 Core Strategy run and its definition review_state from mart.",
    "topt_gppe": "Read the current production TOPT gross-profit-per-employee results and quality report from mart.",
    "metric_node_values": "Read materialized metric-forest node outputs from mart.metric_node_values by node_key at the governed head.",
    "research_report": "Assemble a deterministic research report (company, ETF, or theme ranking) over mart outputs.",
    "research_report_markdown": "Render the deterministic research report of a request as Markdown with value, period, confidence and trace.",
    "research_card": "Render a versioned research card from a freshly-assembled research report over mart outputs.",
    "company_360_profile": "Read an issuer's 360 profile across GPPE, valuation tier, valuation gap, PEG, and theme purity.",
    "theme_purity_leaderboard": "Read the theme purity leaderboard ranking the purest issuers under a thematic definition.",
    "etf_virtual_company_profile": "Read an ETF virtual-company consolidation profile and holdings valuation from mart.",
    "issuer_supply_chain_exposure": "Read module 3 supply-chain exposure of an issuer from the governed head in mart.served_head.",
    "issuer_analyst_ratings": "Read module 4 analyst consensus of an issuer from the governed head in mart.served_head.",
    "governed_backtest": "Read governed backtest execution runs, performance metrics, valuations and simulated trades from mart.",
}


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
    topt_repository: PostgresToptGppeRepository | None = None,
    metric_node_repository: PostgresMetricNodeValueRepository | None = None,
    research_repository: ResearchReadPort | None = None,
    company_profile_reader: CompanyProfileReader | None = None,
    theme_purity_reader: ThemePurityLeaderboardReader | None = None,
    etf_profile_reader: EtfProfileReader | None = None,
    backtest_reader: BacktestReader | None = None,
    issuer_module_reader: IssuerModuleReader | None = None,
) -> FastMCP:
    server = _AuditedFastMCP(
        "truealpha-mcp",
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    url = settings.database_url
    tools = _McpTools(
        repo=repository or _default_repository(),
        topt=topt_repository or PostgresToptGppeRepository(database_url=url),
        metric_node=metric_node_repository or PostgresMetricNodeValueRepository(database_url=url),
        research=research_repository or _default_research_report_repository(),
        company=company_profile_reader or _default_company_profile_reader(),
        theme=theme_purity_reader or _default_theme_purity_reader(),
        etf=etf_profile_reader or _default_etf_profile_reader(),
        backtest=backtest_reader or _default_backtest_reader(),
        modules=issuer_module_reader or _default_issuer_module_reader(),
    )
    for name, desc in _TOOL_DESCRIPTIONS.items():
        server.tool(name=name, description=desc)(getattr(tools, name))
    return server


mcp = build_mcp_server()
