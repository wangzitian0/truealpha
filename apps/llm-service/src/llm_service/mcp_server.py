"""The MCP endpoint — see #348 and #1104.

Registers seven read-only tools: `strategy_run` (#347's provisional
`StrategyRunReadRepository`), `topt_gppe` (#405/#433's TOPT GPPE + quality
read), `research_report` (#369's deterministic report assembler),
`research_card` (#372's deterministic card renderer), `company_360_profile`
(#1104's issuer 360 profile), `theme_purity_leaderboard` (#1104's theme purity
leaderboard), and `etf_virtual_company_profile` (#1104's virtual company profile).

No browser session exists for MCP callers today, so `AccessContext` is
derived server-side via `AuthenticationMethod.SERVICE_IDENTITY` rather than
accepted from the client — the tool schema has no role/tenant/tier argument.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research_cards import CardKind, ResearchCard, build_card
from truealpha_contracts.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
)
from truealpha_contracts.research_report_mart import MartResearchReadRepository
from truealpha_contracts.strategy_run import StrategyRunReadRepository, StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository
from truealpha_contracts.topt_read import (
    PostgresToptGppeRepository,
    ToptGppeReport,
    ToptGppeUnavailable,
)

from llm_service.config import settings

logger = logging.getLogger(__name__)

_SERVICE_PRINCIPAL_ID = "principal:llm-service-mcp"
_SERVICE_TENANT_ID = "tenant:truealpha"
_CONTEXT_LIFETIME = timedelta(minutes=5)

#: #1062 / Rule B: consumer queries on run-addressed mart relations read
#: through the governed head exposed by mart.served_head.
SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head"


class StrategyRunToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    strategy_id: str


class ResearchReportToolRequest(BaseModel):
    # No strict=True (unlike StrategyRunToolRequest, whose sole field is already a bare
    # str): this model's enum/tuple/datetime fields need normal Pydantic coercion from the
    # JSON-shaped tool-call arguments (a JSON string into ResearchReportKind/datetime, a
    # JSON array into a tuple) — strict mode rejects those as wrong-type instances outright
    # rather than coercing them, verified empirically against a real MCP tool call.
    model_config = ConfigDict(extra="forbid")

    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None


class ResearchCardToolRequest(ResearchReportToolRequest):
    """The report request this card is built from, plus the card's own kind. `build_card`
    (#372) takes an already-assembled `ResearchReport` and nothing else, so this tool
    assembles one internally via the exact same path `research_report` uses, then renders
    it — never queries mart directly and computes no new metric of its own."""

    card_kind: CardKind


class CompanyProfileToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer_id: str


class ThemePurityToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme_id: str
    limit: int = 10


class EtfProfileToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: str = "etf:series:S000101292"


def _service_access_context() -> AccessContext:
    """A fresh, short-lived SERVICE_IDENTITY context; never derived from client input."""
    issued_at = datetime.now(UTC)
    return AccessContext(
        context_id=f"ctx:mcp-service:{issued_at.timestamp()}",
        principal_id=_SERVICE_PRINCIPAL_ID,
        tenant_id=_SERVICE_TENANT_ID,
        session_id=f"session:mcp-service:{issued_at.timestamp()}",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=issued_at,
        expires_at=issued_at + _CONTEXT_LIFETIME,
    )


def _default_repository() -> StrategyRunReadRepository:
    """The real mart read, always (#362 made it the default; #434 exit criterion 3
    removed the `strategy_run_backend` flag that could still select the fixture in a
    deployed process). Tests inject a repository through `build_mcp_server`."""
    return PostgresStrategyRunRepository(database_url=settings.database_url)


def _default_research_report_repository() -> ResearchReadPort:
    """#369: the research-report reader wraps the same strategy-run source as
    `_default_repository`, so it is the mart reader, always."""
    return MartResearchReadRepository(database_url=settings.database_url)


class CompanyProfileReader(Protocol):
    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]: ...


class ThemePurityLeaderboardReader(Protocol):
    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]: ...


class EtfProfileReader(Protocol):
    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]: ...


class PostgresCompanyProfileReader:
    """Reads issuer 360 profile from mart tables for MCP consumers."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]:
        decision_row: dict[str, Any] | None = None
        gppe_row: dict[str, Any] | None = None
        themes: list[dict[str, Any]] = []

        cand = resolve_issuer_resolution_candidates(issuer_id)
        clean_issuer = cand["clean_issuer"]
        token = cand["token"]
        upper_issuer = cand["upper_issuer"]
        upper_token = cand["upper_token"]

        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select coalesce(ei.legacy_id, d.issuer_id) as issuer_id,
                               to_char(d.cutoff_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff_at,
                               d.tier,
                               d.valuation_gap::text as valuation_gap,
                               d.current_price_to_sales::text as current_price_to_sales,
                               d.target_price_to_sales::text as target_price_to_sales,
                               d.outcome,
                               d.rank,
                               d.peg::text as peg,
                               d.peg_rank,
                               d.peg_reason_codes
                        from mart.strategy_decisions d
                        left join lateral (
                            select ei.legacy_id
                            from mart.entity_identity ei
                            where ei.entity_id::text = d.issuer_id
                            limit 1
                        ) ei on true
                        where d.issuer_id = %s
                           or d.issuer_id in (
                               select entity_id::text from mart.entity_identity
                               where legacy_id in (%s, %s)
                                  or upper(current_ticker) in (%s, %s)
                                  or upper(legacy_id) in (%s, %s)
                           )
                        order by d.cutoff_at desc
                        limit 1
                        """,
                        (clean_issuer, clean_issuer, token, upper_issuer, upper_token, upper_issuer, upper_token),
                    )
                    decision_row = cur.fetchone()

                    cur.execute(
                        """
                        select gppe::text as gppe,
                               operating_branch,
                               capital_adjusted_gross_profit::text as capital_adjusted_gross_profit,
                               coalesce(availability_status, availability, 'unavailable') as availability_status,
                               reason_codes
                        from mart.topt_gppe_results g
                        where g.issuer_id = %s
                           or g.issuer_id in (
                               select entity_id::text from mart.entity_identity
                               where legacy_id in (%s, %s)
                                  or upper(current_ticker) in (%s, %s)
                                  or upper(legacy_id) in (%s, %s)
                           )
                        order by g.cutoff desc, g.created_at desc
                        limit 1
                        """,
                        (clean_issuer, clean_issuer, token, upper_issuer, upper_token, upper_issuer, upper_token),
                    )
                    gppe_row = cur.fetchone()

                    cur.execute(
                        """
                        select theme_id,
                               theme,
                               theme_share,
                               in_theme_revenue,
                               consolidated_revenue,
                               segments,
                               confidence,
                               availability_status
                        from (
                            select distinct on (p.theme_id)
                                   p.theme_id,
                                   p.theme,
                                   p.theme_share::text as theme_share,
                                   p.theme_share as raw_share,
                                   p.in_theme_revenue::text as in_theme_revenue,
                                   p.consolidated_revenue::text as consolidated_revenue,
                                   p.segments,
                                   p.confidence::text as confidence,
                                   coalesce(p.availability_status, 'unavailable') as availability_status
                            from mart.issuer_theme_purity p
                            where p.issuer_id = %s
                               or p.issuer_id in (
                                   select entity_id::text from mart.entity_identity
                                   where legacy_id in (%s, %s)
                                      or upper(current_ticker) in (%s, %s)
                                      or upper(legacy_id) in (%s, %s)
                               )
                            order by p.theme_id, p.cutoff desc, p.created_at desc
                        ) latest_per_theme
                        order by raw_share desc nulls last, theme asc
                        """,
                        (clean_issuer, clean_issuer, token, upper_issuer, upper_token, upper_issuer, upper_token),
                    )
                    themes = [
                        {
                            "theme_id": str(r.get("theme_id", "")),
                            "theme": str(r.get("theme", "")),
                            "theme_share": str(r["theme_share"]) if r.get("theme_share") is not None else None,
                            "in_theme_revenue": (
                                str(r["in_theme_revenue"]) if r.get("in_theme_revenue") is not None else None
                            ),
                            "consolidated_revenue": (
                                str(r["consolidated_revenue"]) if r.get("consolidated_revenue") is not None else None
                            ),
                            "segments": int(r["segments"]) if r.get("segments") is not None else 0,
                            "confidence": str(r["confidence"]) if r.get("confidence") is not None else None,
                            "availability_status": str(r.get("availability_status", "unavailable")),
                        }
                        for r in cur.fetchall()
                    ]
        except psycopg.Error as exc:
            logger.warning("get_company_profile failed to query database: %s", exc)

        tier = str(decision_row["tier"]) if decision_row and decision_row.get("tier") is not None else None
        valuation_gap = (
            str(decision_row["valuation_gap"])
            if decision_row and decision_row.get("valuation_gap") is not None
            else None
        )
        current_ps = (
            str(decision_row["current_price_to_sales"])
            if decision_row and decision_row.get("current_price_to_sales") is not None
            else None
        )
        target_ps = (
            str(decision_row["target_price_to_sales"])
            if decision_row and decision_row.get("target_price_to_sales") is not None
            else None
        )
        peg = str(decision_row["peg"]) if decision_row and decision_row.get("peg") is not None else None
        peg_rank = int(decision_row["peg_rank"]) if decision_row and decision_row.get("peg_rank") is not None else None
        peg_reason_codes = (
            list(decision_row["peg_reason_codes"])
            if decision_row and decision_row.get("peg_reason_codes") is not None
            else []
        )
        outcome = str(decision_row["outcome"]) if decision_row and decision_row.get("outcome") is not None else None
        cutoff_at = (
            str(decision_row["cutoff_at"]) if decision_row and decision_row.get("cutoff_at") is not None else None
        )

        resolved_issuer_id = (
            str(decision_row["issuer_id"]) if decision_row and decision_row.get("issuer_id") is not None else issuer_id
        )

        gppe_val = str(gppe_row["gppe"]) if gppe_row and gppe_row.get("gppe") is not None else None
        operating_branch = (
            str(gppe_row["operating_branch"]) if gppe_row and gppe_row.get("operating_branch") is not None else None
        )
        capital_adjusted_gross_profit = (
            str(gppe_row["capital_adjusted_gross_profit"])
            if gppe_row and gppe_row.get("capital_adjusted_gross_profit") is not None
            else None
        )
        gppe_availability = (
            str(gppe_row["availability_status"])
            if gppe_row and gppe_row.get("availability_status") is not None
            else "unavailable"
        )
        gppe_reason_codes = (
            list(gppe_row["reason_codes"]) if gppe_row and gppe_row.get("reason_codes") is not None else []
        )

        return {
            "issuer_id": resolved_issuer_id,
            "tier": tier,
            "valuation_gap": valuation_gap,
            "current_price_to_sales": current_ps,
            "target_price_to_sales": target_ps,
            "peg": peg,
            "peg_rank": peg_rank,
            "peg_reason_codes": peg_reason_codes,
            "gppe": gppe_val,
            "gppe_detail": {
                "gppe": gppe_val,
                "operating_branch": operating_branch,
                "capital_adjusted_gross_profit": capital_adjusted_gross_profit,
                "availability_status": gppe_availability,
                "reason_codes": gppe_reason_codes,
            }
            if gppe_row
            else None,
            "strategy_decision": {
                "cutoff_at": cutoff_at,
                "outcome": outcome,
                "tier": tier,
                "valuation_gap": valuation_gap,
                "current_price_to_sales": current_ps,
                "target_price_to_sales": target_ps,
                "peg": peg,
                "peg_rank": peg_rank,
                "peg_reason_codes": peg_reason_codes,
            }
            if decision_row
            else None,
            "theme_purity": themes,
            "availability_status": "available" if (decision_row or gppe_row or themes) else "unavailable",
        }


def normalize_theme_slug(theme_id: str) -> str:
    """Normalize a theme identifier or display name into a hyphenated slug."""
    normalized_slug = theme_id.strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in normalized_slug:
        normalized_slug = normalized_slug.replace("--", "-")
    return normalized_slug


def resolve_fund_resolution_candidates(fund_id: str) -> dict[str, Any]:
    """Resolve candidate identifiers and patterns for ETF profile queries."""
    clean_id = fund_id.strip()
    token = clean_id.split(":")[-1].split("/")[-1].strip()
    upper_token = token.upper()
    lower_token = token.lower()
    clean_upper = clean_id.upper()
    clean_lower = clean_id.lower()
    series_candidate = f"etf:series:{upper_token}" if not clean_lower.startswith("etf:series:") else clean_id
    enable_name_match = len(lower_token) >= 2
    name_pattern = f"%{lower_token}%" if enable_name_match else ""
    return {
        "clean_id": clean_id,
        "series_candidate": series_candidate,
        "upper_token": upper_token,
        "clean_upper": clean_upper,
        "name_pattern": name_pattern,
    }


def resolve_issuer_resolution_candidates(issuer_id: str) -> dict[str, str]:
    """Resolve candidate identifiers for issuer company 360 queries."""
    clean_issuer = issuer_id.strip()
    token = clean_issuer.split(":")[-1].strip()
    return {
        "clean_issuer": clean_issuer,
        "token": token,
        "upper_issuer": clean_issuer.upper(),
        "upper_token": token.upper(),
    }


class PostgresThemePurityLeaderboardReader:
    """Reads theme purity leaderboard from mart tables for MCP consumers."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]:
        issuers: list[dict[str, Any]] = []
        clamped_limit = max(1, min(limit, 100))
        normalized_slug = normalize_theme_slug(theme_id)

        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        with latest_per_issuer as (
                            select distinct on (p.issuer_id)
                                   p.issuer_id,
                                   p.cik,
                                   p.theme_id,
                                   p.theme,
                                   p.theme_share,
                                   p.in_theme_revenue,
                                   p.consolidated_revenue,
                                   p.segments,
                                   p.confidence,
                                   coalesce(p.availability_status, 'unavailable') as availability_status,
                                   p.cutoff,
                                   p.created_at
                            from mart.issuer_theme_purity p
                            where p.theme_id = %s or p.theme_id = %s or lower(p.theme) = lower(%s)
                            order by p.issuer_id, p.cutoff desc, p.created_at desc
                        )
                        select coalesce(ei.legacy_id, l.issuer_id) as issuer_id,
                               l.cik,
                               l.theme_id,
                               l.theme,
                               l.theme_share::text as theme_share,
                               l.in_theme_revenue::text as in_theme_revenue,
                               l.consolidated_revenue::text as consolidated_revenue,
                               l.segments,
                               l.confidence::text as confidence,
                               l.availability_status
                        from latest_per_issuer l
                        left join lateral (
                            select ei.legacy_id
                            from mart.entity_identity ei
                            where ei.entity_id::text = l.issuer_id
                            limit 1
                        ) ei on true
                        order by (l.theme_share) desc nulls last, (l.confidence) desc nulls last
                        limit %s
                        """,
                        (theme_id, normalized_slug, theme_id.strip(), clamped_limit),
                    )
                    issuers = [
                        {
                            "issuer_id": str(r["issuer_id"]),
                            "cik": int(r["cik"]) if r.get("cik") is not None else None,
                            "theme_id": str(r["theme_id"]),
                            "theme": str(r["theme"]),
                            "theme_share": str(r["theme_share"]) if r.get("theme_share") is not None else None,
                            "in_theme_revenue": (
                                str(r["in_theme_revenue"]) if r.get("in_theme_revenue") is not None else None
                            ),
                            "consolidated_revenue": (
                                str(r["consolidated_revenue"]) if r.get("consolidated_revenue") is not None else None
                            ),
                            "segments": int(r["segments"]) if r.get("segments") is not None else 0,
                            "confidence": str(r["confidence"]) if r.get("confidence") is not None else None,
                            "availability_status": str(r.get("availability_status", "unavailable")),
                        }
                        for r in cur.fetchall()
                    ]
        except psycopg.Error as exc:
            logger.warning("get_leaderboard failed to query database: %s", exc)

        return {
            "theme_id": theme_id,
            "limit": clamped_limit,
            "count": len(issuers),
            "issuers": issuers,
        }


class PostgresEtfProfileReader:
    """Reads ETF virtual-company consolidation profile from mart tables for MCP consumers."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]:
        fund_row: dict[str, Any] | None = None
        holdings: list[dict[str, Any]] = []

        query_params = resolve_fund_resolution_candidates(fund_id)

        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select fund_id,
                               coalesce(fund_name, fund_id) as fund_name,
                               to_char(cutoff, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
                               to_char(report_period, 'YYYY-MM-DD') as report_period,
                               weighted_valuation_gap::text as weighted_valuation_gap,
                               total_weight_pct::text as total_weight_pct,
                               resolved_weight_pct::text as resolved_weight_pct,
                               valued_weight_pct::text as valued_weight_pct,
                               lines,
                               valued_lines,
                               confidence::text as confidence,
                               coalesce(availability_status, 'unavailable') as availability_status,
                               reason_codes
                        from mart.fund_virtual_company
                        where fund_id = %(clean_id)s
                           or fund_id = %(series_candidate)s
                           or fund_id in (
                               select entity_id
                               from staging.kg_identifiers
                               where (identifier_type = 'ticker' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                  or (identifier_type = 'sec_series' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                           )
                           or fund_id in (
                               select entity_id::text
                               from mart.entity_identity
                               where upper(current_ticker) in (%(upper_token)s, %(clean_upper)s)
                                  or upper(legacy_id) in (%(upper_token)s, %(clean_upper)s)
                           )
                           or (%(name_pattern)s <> '' and lower(fund_name) like %(name_pattern)s)
                           or (%(name_pattern)s <> '' and lower(fund_id) like %(name_pattern)s)
                        order by
                            case
                                when fund_id = %(clean_id)s or fund_id = %(series_candidate)s then 1
                                when fund_id in (
                                    select entity_id from staging.kg_identifiers
                                    where (identifier_type = 'ticker' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                       or (identifier_type = 'sec_series' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                ) then 2
                                when fund_id in (
                                    select entity_id::text from mart.entity_identity
                                    where upper(current_ticker) in (%(upper_token)s, %(clean_upper)s)
                                       or upper(legacy_id) in (%(upper_token)s, %(clean_upper)s)
                                ) then 3
                                else 4
                            end asc,
                            cutoff desc,
                            created_at desc
                        limit 1
                        """,
                        query_params,
                    )
                    fund_row = cur.fetchone()
                    target_fund_id = str(fund_row["fund_id"]) if fund_row else query_params["clean_id"]
                    holdings_params = dict(query_params)
                    holdings_params["target_fund_id"] = target_fund_id

                    cur.execute(
                        """
                        select coalesce(fund_name, fund_id) as fund_name,
                               to_char(report_period, 'YYYY-MM-DD') as report_period,
                               holding_name,
                               ticker,
                               isin,
                               percent_of_net_assets::text as weight_pct,
                               value_usd::text as value_usd,
                               coalesce(ei.legacy_id, v.issuer_entity) as issuer_entity,
                               v.listing_id
                        from mart.fund_holdings_valuation v
                        left join lateral (
                            select ei.legacy_id
                            from mart.entity_identity ei
                            where ei.entity_id::text = v.issuer_entity
                            limit 1
                        ) ei on true
                        where v.fund_id = %(target_fund_id)s
                           or v.fund_id = %(series_candidate)s
                           or v.fund_id in (
                               select entity_id
                               from staging.kg_identifiers
                               where (identifier_type = 'ticker' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                  or (identifier_type = 'sec_series' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                           )
                           or v.fund_id in (
                               select entity_id::text
                               from mart.entity_identity
                               where upper(current_ticker) in (%(upper_token)s, %(clean_upper)s)
                                  or upper(legacy_id) in (%(upper_token)s, %(clean_upper)s)
                           )
                           or (%(name_pattern)s <> '' and lower(v.fund_name) like %(name_pattern)s)
                           or (%(name_pattern)s <> '' and lower(v.fund_id) like %(name_pattern)s)
                        order by
                            case
                                when v.fund_id = %(target_fund_id)s or v.fund_id = %(series_candidate)s then 1
                                when v.fund_id in (
                                    select entity_id from staging.kg_identifiers
                                    where (identifier_type = 'ticker' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                       or (identifier_type = 'sec_series' and upper(identifier_value) in (%(upper_token)s, %(clean_upper)s))
                                ) then 2
                                else 3
                            end asc,
                            v.percent_of_net_assets desc nulls last,
                            v.holding_name asc
                        limit 100
                        """,
                        holdings_params,
                    )
                    holdings = [
                        {
                            "holding_name": str(r.get("holding_name", "")),
                            "ticker": str(r["ticker"]) if r.get("ticker") is not None else None,
                            "isin": str(r["isin"]) if r.get("isin") is not None else None,
                            "weight_pct": str(r["weight_pct"]) if r.get("weight_pct") is not None else None,
                            "value_usd": str(r["value_usd"]) if r.get("value_usd") is not None else None,
                            "issuer_entity": (str(r["issuer_entity"]) if r.get("issuer_entity") is not None else None),
                            "listing_id": str(r["listing_id"]) if r.get("listing_id") is not None else None,
                        }
                        for r in cur.fetchall()
                    ]
        except psycopg.Error as exc:
            logger.warning("get_etf_profile failed to query database: %s", exc)

        fund_name = (
            str(fund_row["fund_name"])
            if fund_row and fund_row.get("fund_name") is not None
            else (str(holdings[0]["fund_name"]) if holdings and "fund_name" in holdings[0] else fund_id)
        )
        cutoff = str(fund_row["cutoff"]) if fund_row and fund_row.get("cutoff") is not None else None
        report_period = (
            str(fund_row["report_period"])
            if fund_row and fund_row.get("report_period") is not None
            else (str(holdings[0]["report_period"]) if holdings and "report_period" in holdings[0] else None)
        )
        weighted_gap = (
            str(fund_row["weighted_valuation_gap"])
            if fund_row and fund_row.get("weighted_valuation_gap") is not None
            else None
        )
        total_weight = (
            str(fund_row["total_weight_pct"]) if fund_row and fund_row.get("total_weight_pct") is not None else "0.00"
        )
        resolved_weight = (
            str(fund_row["resolved_weight_pct"])
            if fund_row and fund_row.get("resolved_weight_pct") is not None
            else "0.00"
        )
        valued_weight = (
            str(fund_row["valued_weight_pct"]) if fund_row and fund_row.get("valued_weight_pct") is not None else "0.00"
        )
        lines_count = int(fund_row["lines"]) if fund_row and fund_row.get("lines") is not None else len(holdings)
        valued_lines_count = (
            int(fund_row["valued_lines"]) if fund_row and fund_row.get("valued_lines") is not None else 0
        )
        confidence_val = str(fund_row["confidence"]) if fund_row and fund_row.get("confidence") is not None else None
        availability_status = (
            str(fund_row["availability_status"])
            if fund_row and fund_row.get("availability_status") is not None
            else ("available" if holdings else "unavailable")
        )
        reason_codes = (
            list(fund_row["reason_codes"])
            if fund_row and fund_row.get("reason_codes") is not None
            else ([] if holdings else ["no_virtual_company_consolidation_found"])
        )

        return {
            "fund_id": fund_id,
            "fund_name": fund_name,
            "cutoff": cutoff,
            "report_period": report_period,
            "weighted_valuation_gap": weighted_gap,
            "total_weight_pct": total_weight,
            "resolved_weight_pct": resolved_weight,
            "valued_weight_pct": valued_weight,
            "lines": lines_count,
            "valued_lines": valued_lines_count,
            "confidence": confidence_val,
            "availability_status": availability_status,
            "reason_codes": reason_codes,
            "holdings": holdings,
        }


def _default_company_profile_reader() -> CompanyProfileReader:
    return PostgresCompanyProfileReader(database_url=settings.database_url)


def _default_theme_purity_reader() -> ThemePurityLeaderboardReader:
    return PostgresThemePurityLeaderboardReader(database_url=settings.database_url)


def _default_etf_profile_reader() -> EtfProfileReader:
    return PostgresEtfProfileReader(database_url=settings.database_url)


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
    topt_repository: PostgresToptGppeRepository | None = None,
    research_repository: ResearchReadPort | None = None,
    company_profile_reader: CompanyProfileReader | None = None,
    theme_purity_reader: ThemePurityLeaderboardReader | None = None,
    etf_profile_reader: EtfProfileReader | None = None,
) -> FastMCP:
    """Builds the MCP server. Caller-supplied repositories are for tests only."""
    # `streamable_http_path="/"`: main.py mounts this app at "/mcp"; FastMCP's own
    # default streamable path is also "/mcp", which double-nested the real endpoint at
    # "/mcp/mcp". Serving at the mount root keeps it a single "/mcp".
    # transport_security: see Settings.mcp_dns_rebinding_protection for why host pinning
    # is off by default for this service-identity-only surface behind Traefik.
    server = FastMCP(
        "truealpha-mcp",
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    active_repository: StrategyRunReadRepository = repository if repository is not None else _default_repository()
    active_topt = (
        topt_repository
        if topt_repository is not None
        else PostgresToptGppeRepository(database_url=settings.database_url)
    )
    active_research: ResearchReadPort = (
        research_repository if research_repository is not None else _default_research_report_repository()
    )
    active_company_profile: CompanyProfileReader = (
        company_profile_reader if company_profile_reader is not None else _default_company_profile_reader()
    )
    active_theme_purity: ThemePurityLeaderboardReader = (
        theme_purity_reader if theme_purity_reader is not None else _default_theme_purity_reader()
    )
    active_etf_profile: EtfProfileReader = (
        etf_profile_reader if etf_profile_reader is not None else _default_etf_profile_reader()
    )

    @server.tool(name="strategy_run", description="Read the latest large_model_value_v0 Core Strategy run.")
    def strategy_run(request: StrategyRunToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return active_repository.get_latest(strategy_id=request.strategy_id, context=_service_access_context())

    @server.tool(
        name="topt_gppe",
        description="Read the current production TOPT gross-profit-per-employee results and quality report from mart.",
    )
    def topt_gppe() -> ToptGppeReport | ToptGppeUnavailable:
        return active_topt.latest()

    @server.tool(
        name="research_report",
        description=(
            "Assemble a deterministic research report (company, ETF, or theme ranking) by selecting "
            "already-materialized sections and trace links over mart outputs. Computes no new metric."
        ),
    )
    def research_report(request: ResearchReportToolRequest) -> ResearchReport:
        report_request = ResearchReportRequest(
            report_kind=request.report_kind,
            target_entity_ids=request.target_entity_ids,
            cutoff_at=request.cutoff_at,
            section_kinds=request.section_kinds,
            strategy_id=request.strategy_id,
            title=request.title,
        )
        return build_research_report(report_request, active_research, context=_service_access_context())

    @server.tool(
        name="research_card",
        description=(
            "Render a versioned research card (company, comparison, ranking, ETF, "
            "supply-chain, or strategy-summary) from a freshly-assembled research report. "
            "Pure transform over the report: computes no new metric, ranking, or "
            "classification, and queries nothing beyond the report it builds first."
        ),
    )
    def research_card(request: ResearchCardToolRequest) -> ResearchCard:
        report_request = ResearchReportRequest(
            report_kind=request.report_kind,
            target_entity_ids=request.target_entity_ids,
            cutoff_at=request.cutoff_at,
            section_kinds=request.section_kinds,
            strategy_id=request.strategy_id,
            title=request.title,
        )
        report = build_research_report(report_request, active_research, context=_service_access_context())
        return build_card(report, request.card_kind, title=request.title)

    @server.tool(
        name="company_360_profile",
        description=(
            "Read an issuer's comprehensive 360 profile across gross-profit-per-employee (GPPE), "
            "three-tier valuation tag, valuation gap, historical CAGR PEG, and theme purity."
        ),
    )
    def company_360_profile(request: CompanyProfileToolRequest) -> dict[str, Any]:
        return active_company_profile.get_company_profile(issuer_id=request.issuer_id)

    @server.tool(
        name="theme_purity_leaderboard",
        description=("Read the theme purity leaderboard ranking the purest issuers under a given thematic definition."),
    )
    def theme_purity_leaderboard(request: ThemePurityToolRequest) -> dict[str, Any]:
        return active_theme_purity.get_leaderboard(theme_id=request.theme_id, limit=request.limit)

    @server.tool(
        name="etf_virtual_company_profile",
        description=("Read an ETF virtual-company consolidation profile and holdings valuation from mart."),
    )
    def etf_virtual_company_profile(request: EtfProfileToolRequest) -> dict[str, Any]:
        return active_etf_profile.get_etf_profile(fund_id=request.fund_id)

    return server


mcp = build_mcp_server()
