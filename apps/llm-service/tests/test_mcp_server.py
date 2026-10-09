from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from llm_service.mcp_server import _default_repository, build_mcp_server, mcp
from mcp.shared.memory import create_connected_server_and_client_session
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.research_cards import ResearchCard
from truealpha_contracts.research_report import ResearchReport
from truealpha_contracts.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository


class _RecordingRepository:
    """Captures the AccessContext it was called with; returns a fixed unavailable result."""

    def __init__(self) -> None:
        self.received_contexts: list[AccessContext] = []

    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunUnavailable:
        self.received_contexts.append(context)
        return StrategyRunUnavailable(strategy_id=strategy_id, reason="unknown_strategy_id")


@pytest.mark.anyio
async def test_advertises_the_expected_tools() -> None:
    tools = await mcp.list_tools()
    assert sorted(tool.name for tool in tools) == [
        "company_360_profile",
        "etf_virtual_company_profile",
        "research_card",
        "research_report",
        "strategy_run",
        "theme_purity_leaderboard",
        "topt_gppe",
    ]
    strategy_tool = next(t for t in tools if t.name == "strategy_run")
    assert strategy_tool.inputSchema["required"] == ["request"]
    assert strategy_tool.outputSchema is not None
    assert "result" in strategy_tool.outputSchema["properties"]


@pytest.mark.anyio
async def test_topt_gppe_tool_serializes_whatever_repository_its_given() -> None:
    """Proves the tool's wiring/serialization only -- an injected fake, not
    PostgresToptGppeRepository's own SQL/row-materialization. That class has its
    own real-Postgres coverage in libs/contracts/tests/test_topt_read.py; a
    previous version of this test's name (test_topt_gppe_reads_the_mart_repository
    _not_a_fixture) implied it covered that class's DB logic, which it never did
    -- see truealpha#462, where that misleading name was part of how #461's
    KeyError bug shipped without a real regression test catching it."""
    from truealpha_contracts.topt_read import ToptGppeCell, ToptGppeReport

    class _FakeTopt:
        def latest(self, *, limit: int = 100) -> ToptGppeReport:
            return ToptGppeReport(
                run_id="capture-run:" + "a" * 64,
                requested_count=84,
                available_count=1,
                cells=(
                    ToptGppeCell(
                        listing_id="listing:xnas:goog",
                        availability="available",
                        gppe="1153614.48",
                        confidence="0.85",
                    ),
                ),
                quality={"denominator_mean_confidence": "0.9171"},
            )

    server = build_mcp_server(repository=FixtureStrategyRunRepository(), topt_repository=_FakeTopt())
    content_blocks, structured = await server.call_tool("topt_gppe", {})  # type: ignore[misc]
    report = ToptGppeReport.model_validate_json(json.dumps(structured["result"]))  # type: ignore[index]
    assert report.available_count == 1
    assert report.cells[0].listing_id == "listing:xnas:goog"
    assert report.cells[0].gppe == "1153614.48"


@pytest.mark.anyio
async def test_research_report_reads_through_the_injected_repository() -> None:
    """#369: the tool assembles over whatever ResearchReadPort it's given — proving the
    deployed path invokes build_research_report at all, independent of #26's writer."""
    from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository

    server = build_mcp_server(
        repository=FixtureStrategyRunRepository(), research_repository=FixtureResearchReadRepository()
    )
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "research_report",
        {
            "request": {
                "report_kind": "company",
                "target_entity_ids": ["issuer:adm"],
                "cutoff_at": "2026-06-30T23:59:59Z",
            }
        },
    )
    assert content_blocks
    # Unlike strategy_run/topt_gppe (Report | Unavailable unions, wrapped under a
    # top-level "result" key), a tool whose return type is a single concrete BaseModel
    # gets its own fields flattened directly into `structured` — verified empirically.
    # JSON-mode validation (not model_validate on the raw dict): ResearchReport is a
    # strict model, and only JSON-mode coerces a wire string into datetime/enum/tuple.
    report = ResearchReport.model_validate_json(json.dumps(structured))
    assert report.report_id.startswith("report:")
    assert report.generated_from == "fixture:research_report.v1"
    assert report.subjects[0].subject_id == "issuer:adm"


@pytest.mark.anyio
async def test_research_card_renders_from_a_freshly_built_report() -> None:
    """#372/#434 track C: research_card is the renderer's first deployed consumer --
    proves the deployed path invokes build_card at all over a real ResearchReport."""
    from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository

    server = build_mcp_server(
        repository=FixtureStrategyRunRepository(), research_repository=FixtureResearchReadRepository()
    )
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "research_card",
        {
            "request": {
                "report_kind": "company",
                "target_entity_ids": ["issuer:adm"],
                "cutoff_at": "2026-06-30T23:59:59Z",
                "card_kind": "company",
            }
        },
    )
    assert content_blocks
    # Same flattened-fields shape as research_report (single concrete BaseModel return).
    card = ResearchCard.model_validate_json(json.dumps(structured))
    assert card.card_id.startswith("card:")
    assert card.card_kind.value == "company"
    assert card.generated_from_report_id.startswith("report:")


def test_default_research_report_repository_is_mart_backed_with_fixture_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#369 / #434 criterion 3: the research-report reader defaults to the mart reader,
    exactly like the strategy-run reader, and there is no flag to select the fixture —
    a fixture reader exists only by explicit injection in a test."""
    from llm_service import mcp_server
    from llm_service.mcp_server import _default_research_report_repository
    from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository
    from truealpha_contracts.research_report_mart import MartResearchReadRepository

    # #434 exit criterion 3: there is no flag. The default is the mart reader, and the
    # fixture reader is reachable only by explicit injection in a test.
    assert not hasattr(mcp_server.settings, "strategy_run_backend")
    assert isinstance(_default_research_report_repository(), MartResearchReadRepository)
    assert FixtureResearchReadRepository is not None  # the test double still exists, for tests


@pytest.mark.anyio
async def test_tool_reads_through_the_shared_repository_and_matches_the_fixture() -> None:
    # The shipped default is now the mart read (#362); this test pins the fixture
    # oracle explicitly, so build a fixture-backed server rather than the
    # module-level `mcp`.
    server = build_mcp_server(repository=FixtureStrategyRunRepository())
    # FastMCP.call_tool's declared return type doesn't reflect that it actually
    # returns a (content_blocks, structured_dict) tuple when structured output
    # is enabled (verified at runtime); silence the resulting stub mismatch.
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "strategy_run", {"request": {"strategy_id": "large_model_value_v0"}}
    )
    raw_result = structured["result"]  # type: ignore[index]
    assert raw_result["strategy_run_id"] == "strategy_smoke_fixture"
    assert "governed" in raw_result and raw_result["governed"] is False
    # JSON-mode validation: the wire payload is JSON-native (lists), not Python tuples.
    report = StrategyRunReport.model_validate_json(json.dumps(raw_result))
    assert report.strategy_id == "large_model_value_v0"
    assert report.strategy_run_id == "strategy_smoke_fixture"
    assert report.governed is False
    selected = next(d for d in report.decisions if d.issuer_id == "issuer:adm" and d.cutoff_at.month == 3)
    assert selected.outcome.value == "selected"
    assert str(selected.valuation_gap) == "1.6388"
    assert selected.confidence is not None


@pytest.mark.anyio
async def test_unknown_strategy_returns_structured_unavailable_not_a_crash() -> None:
    server = build_mcp_server(repository=FixtureStrategyRunRepository())
    _content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "strategy_run", {"request": {"strategy_id": "does_not_exist"}}
    )
    unavailable = StrategyRunUnavailable.model_validate(structured["result"])  # type: ignore[index]
    assert unavailable.reason == "unknown_strategy_id"


@pytest.mark.anyio
async def test_context_is_derived_server_side_via_service_identity_and_not_client_supplied() -> None:
    repository = _RecordingRepository()
    server = build_mcp_server(repository=repository)
    await server.call_tool("strategy_run", {"request": {"strategy_id": "large_model_value_v0"}})

    assert len(repository.received_contexts) == 1
    context = repository.received_contexts[0]
    assert context.authentication_method is AuthenticationMethod.SERVICE_IDENTITY
    assert context.principal_kind is PrincipalKind.SERVICE
    assert context.issued_at <= datetime.now(UTC)
    assert context.expires_at - context.issued_at <= timedelta(minutes=5)

    # The tool's own input schema has no identity/role/tenant argument at all.
    tools = await server.list_tools()
    request_schema = tools[0].inputSchema["$defs"]["StrategyRunToolRequest"]
    assert set(request_schema["properties"]) == {"strategy_id"}


@pytest.mark.anyio
async def test_claude_compatible_client_session_round_trip() -> None:
    """A real mcp.ClientSession over in-memory transport — the same JSON-RPC
    surface Claude Code / Claude Desktop / Codex speak, not a server-side
    shortcut method."""
    # Fixture-backed (the shipped default is now the mart read, #362) so the
    # round-trip asserts against the 10 golden decisions.
    server = build_mcp_server(repository=FixtureStrategyRunRepository())
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert sorted(tool.name for tool in tools.tools) == [
            "company_360_profile",
            "etf_virtual_company_profile",
            "research_card",
            "research_report",
            "strategy_run",
            "theme_purity_leaderboard",
            "topt_gppe",
        ]

        result = await client.call_tool("strategy_run", {"request": {"strategy_id": "large_model_value_v0"}})
        assert result.isError is not True
        assert result.structuredContent is not None
        raw_result = result.structuredContent["result"]
        assert raw_result["strategy_run_id"] == "strategy_smoke_fixture"
        assert "governed" in raw_result and raw_result["governed"] is False
        report = StrategyRunReport.model_validate_json(json.dumps(raw_result))
        assert len(report.decisions) == 10
        assert report.golden_mismatches == ()
        assert report.strategy_run_id == "strategy_smoke_fixture"
        assert report.governed is False


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_streamable_transport_is_reachable_behind_a_proxy() -> None:
    """The MCP is mounted at /mcp and served behind Traefik. Regression guard:
    (1) streamable path is the mount root '/' so the endpoint is a single '/mcp',
    not the double-nested '/mcp/mcp'; (2) DNS-rebinding host pinning is off by
    default so a non-localhost Host does not get rejected with 421 (see #405)."""
    server = build_mcp_server(repository=_RecordingRepository())
    assert server.settings.streamable_http_path == "/"
    assert server.settings.transport_security is not None
    assert server.settings.transport_security.enable_dns_rebinding_protection is False


def test_default_repository_is_mart_backed_with_fixture_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """#362: the shipped default is now `mart` (a real writer populates it via #414/#417);
    `fixture` remains selectable for tests/offline previews."""
    from llm_service import mcp_server

    # The shipped default, with no override, is the real mart read.
    assert isinstance(_default_repository(), PostgresStrategyRunRepository)

    # #434 exit criterion 3: no runtime flag can select the fixture any more.
    assert not hasattr(mcp_server.settings, "strategy_run_backend")
    assert FixtureStrategyRunRepository is not None  # injected by tests only


def test_default_company_and_theme_and_etf_readers_are_postgres_backed() -> None:
    from llm_service.mcp_server import (
        PostgresCompanyProfileReader,
        PostgresEtfProfileReader,
        PostgresThemePurityLeaderboardReader,
        _default_company_profile_reader,
        _default_etf_profile_reader,
        _default_theme_purity_reader,
    )

    assert isinstance(_default_company_profile_reader(), PostgresCompanyProfileReader)
    assert isinstance(_default_theme_purity_reader(), PostgresThemePurityLeaderboardReader)
    assert isinstance(_default_etf_profile_reader(), PostgresEtfProfileReader)


@pytest.mark.anyio
async def test_company_360_profile_tool_reads_through_injected_reader() -> None:
    class _FakeCompanyProfileReader:
        def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]:
            return {
                "issuer_id": issuer_id,
                "tier": "Tier 1: Core Value",
                "valuation_gap": "1.6388",
                "current_price_to_sales": "10.5",
                "target_price_to_sales": "17.2",
                "peg": "1.25",
                "peg_rank": 3,
                "peg_reason_codes": [],
                "gppe": "1153614.48",
                "gppe_detail": {
                    "gppe": "1153614.48",
                    "operating_branch": "tech",
                    "capital_adjusted_gross_profit": "5000000.00",
                    "availability_status": "available",
                    "reason_codes": [],
                },
                "strategy_decision": {
                    "cutoff_at": "2026-03-31T23:59:59Z",
                    "outcome": "selected",
                    "tier": "Tier 1: Core Value",
                    "valuation_gap": "1.6388",
                    "current_price_to_sales": "10.5",
                    "target_price_to_sales": "17.2",
                    "peg": "1.25",
                    "peg_rank": 3,
                    "peg_reason_codes": [],
                },
                "theme_purity": [
                    {
                        "theme_id": "ai-infrastructure",
                        "theme": "AI infrastructure",
                        "theme_share": "0.85",
                        "in_theme_revenue": "850000000.00",
                        "consolidated_revenue": "1000000000.00",
                        "segments": 3,
                        "confidence": "0.95",
                        "availability_status": "available",
                    }
                ],
                "availability_status": "available",
            }

    server = build_mcp_server(
        repository=FixtureStrategyRunRepository(),
        company_profile_reader=_FakeCompanyProfileReader(),
    )
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "company_360_profile",
        {"request": {"issuer_id": "issuer:adm"}},
    )
    assert content_blocks
    assert structured["issuer_id"] == "issuer:adm"
    assert structured["tier"] == "Tier 1: Core Value"
    assert structured["valuation_gap"] == "1.6388"
    assert structured["peg"] == "1.25"
    assert structured["gppe"] == "1153614.48"
    assert structured["theme_purity"][0]["theme_id"] == "ai-infrastructure"
    assert structured["theme_purity"][0]["theme_share"] == "0.85"


@pytest.mark.anyio
async def test_theme_purity_leaderboard_tool_reads_through_injected_reader() -> None:
    class _FakeThemePurityLeaderboardReader:
        def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]:
            return {
                "theme_id": theme_id,
                "limit": limit,
                "count": 1,
                "issuers": [
                    {
                        "issuer_id": "issuer:nvda",
                        "cik": 1045810,
                        "theme_id": theme_id,
                        "theme": "AI infrastructure",
                        "theme_share": "0.92",
                        "in_theme_revenue": "26000000000.00",
                        "consolidated_revenue": "28000000000.00",
                        "segments": 2,
                        "confidence": "0.98",
                        "availability_status": "available",
                    }
                ],
            }

    server = build_mcp_server(
        repository=FixtureStrategyRunRepository(),
        theme_purity_reader=_FakeThemePurityLeaderboardReader(),
    )
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "theme_purity_leaderboard",
        {"request": {"theme_id": "ai-infrastructure", "limit": 5}},
    )
    assert content_blocks
    assert structured["theme_id"] == "ai-infrastructure"
    assert structured["limit"] == 5
    assert structured["count"] == 1
    assert structured["issuers"][0]["issuer_id"] == "issuer:nvda"
    assert structured["issuers"][0]["theme_share"] == "0.92"


@pytest.mark.anyio
async def test_etf_virtual_company_profile_tool_reads_through_injected_reader() -> None:
    class _FakeEtfProfileReader:
        def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]:
            return {
                "fund_id": fund_id,
                "fund_name": "Invesco QQQ Trust",
                "cutoff": "2026-03-31T23:59:59Z",
                "report_period": "2026-03-31",
                "weighted_valuation_gap": "1.4250",
                "total_weight_pct": "99.80",
                "resolved_weight_pct": "95.50",
                "valued_weight_pct": "88.20",
                "lines": 101,
                "valued_lines": 84,
                "confidence": "0.92",
                "availability_status": "available",
                "reason_codes": [],
                "holdings": [
                    {
                        "holding_name": "Apple Inc.",
                        "ticker": "AAPL",
                        "isin": "US0378331005",
                        "weight_pct": "8.85",
                        "value_usd": "25000000000.00",
                        "issuer_entity": "issuer:aapl",
                        "listing_id": "listing:xnas:aapl",
                    }
                ],
            }

    server = build_mcp_server(
        repository=FixtureStrategyRunRepository(),
        etf_profile_reader=_FakeEtfProfileReader(),
    )
    content_blocks, structured = await server.call_tool(  # type: ignore[misc]
        "etf_virtual_company_profile",
        {"request": {"fund_id": "etf:series:S000101292"}},
    )
    assert content_blocks
    assert structured["fund_id"] == "etf:series:S000101292"
    assert structured["fund_name"] == "Invesco QQQ Trust"
    assert structured["weighted_valuation_gap"] == "1.4250"
    assert structured["valued_weight_pct"] == "88.20"
    assert len(structured["holdings"]) == 1
    assert structured["holdings"][0]["ticker"] == "AAPL"


def test_postgres_theme_purity_leaderboard_reader_normalizes_theme_slug() -> None:
    from llm_service.mcp_server import PostgresThemePurityLeaderboardReader

    # Disconnected DB should return empty list gracefully without unhandled exception
    reader = PostgresThemePurityLeaderboardReader(database_url="postgresql://invalid:invalid@127.0.0.1:59999/none")
    result = reader.get_leaderboard(theme_id="AI Infrastructure", limit=5)
    assert result["theme_id"] == "AI Infrastructure"
    assert result["limit"] == 5
    assert result["count"] == 0
    assert result["issuers"] == []


def test_normalize_theme_slug_falsifiable() -> None:
    from llm_service.mcp_server import normalize_theme_slug

    assert normalize_theme_slug("AI Infrastructure") == "ai-infrastructure"
    assert normalize_theme_slug("Cloud   Software--SaaS_Platform") == "cloud-software-saas-platform"
    assert normalize_theme_slug("  semiconductors  ") == "semiconductors"
    assert normalize_theme_slug("") == ""


def test_resolve_fund_resolution_candidates_falsifiable() -> None:
    from llm_service.mcp_server import resolve_fund_resolution_candidates

    qqq = resolve_fund_resolution_candidates("fund:nasdaq:qqq")
    assert qqq["clean_id"] == "fund:nasdaq:qqq"
    assert qqq["upper_token"] == "QQQ"
    assert qqq["series_candidate"] == "etf:series:QQQ"
    assert qqq["name_pattern"] == "%qqq%"

    series = resolve_fund_resolution_candidates("etf:series:S000101292")
    assert series["clean_id"] == "etf:series:S000101292"
    assert series["upper_token"] == "S000101292"
    assert series["series_candidate"] == "etf:series:S000101292"
    assert series["name_pattern"] == "%s000101292%"

    # Short token guard prevents matching broad substrings
    short = resolve_fund_resolution_candidates("q")
    assert short["upper_token"] == "Q"
    assert short["name_pattern"] == ""

    empty = resolve_fund_resolution_candidates("")
    assert empty["upper_token"] == ""
    assert empty["name_pattern"] == ""


def test_resolve_issuer_resolution_candidates_falsifiable() -> None:
    from llm_service.mcp_server import resolve_issuer_resolution_candidates

    nvda = resolve_issuer_resolution_candidates("NVDA")
    assert nvda["clean_issuer"] == "NVDA"
    assert nvda["token"] == "NVDA"
    assert nvda["upper_token"] == "NVDA"

    cik = resolve_issuer_resolution_candidates("issuer:cik:0001045810")
    assert cik["clean_issuer"] == "issuer:cik:0001045810"
    assert cik["token"] == "0001045810"
    assert cik["upper_token"] == "0001045810"


def test_postgres_company_profile_reader_handles_connection_error() -> None:
    from llm_service.mcp_server import PostgresCompanyProfileReader

    reader = PostgresCompanyProfileReader(database_url="postgresql://invalid:invalid@127.0.0.1:59999/none")
    result = reader.get_company_profile(issuer_id="NVDA")
    assert result["issuer_id"] == "NVDA"
    assert result["availability_status"] == "unavailable"
    assert result["theme_purity"] == []


def test_postgres_etf_profile_reader_handles_connection_error() -> None:
    from llm_service.mcp_server import PostgresEtfProfileReader

    reader = PostgresEtfProfileReader(database_url="postgresql://invalid:invalid@127.0.0.1:59999/none")
    result = reader.get_etf_profile(fund_id="fund:nasdaq:qqq")
    assert result["fund_id"] == "fund:nasdaq:qqq"
    assert result["availability_status"] == "unavailable"
    assert result["reason_codes"] == ["no_virtual_company_consolidation_found"]
    assert result["holdings"] == []
