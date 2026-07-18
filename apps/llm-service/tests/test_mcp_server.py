from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from llm_service.mcp_server import _default_repository, build_mcp_server, mcp
from mcp.shared.memory import create_connected_server_and_client_session
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
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
    assert sorted(tool.name for tool in tools) == ["strategy_run", "topt_gppe"]
    strategy_tool = next(t for t in tools if t.name == "strategy_run")
    assert strategy_tool.inputSchema["required"] == ["request"]
    assert strategy_tool.outputSchema is not None
    assert "result" in strategy_tool.outputSchema["properties"]


@pytest.mark.anyio
async def test_topt_gppe_reads_the_mart_repository_not_a_fixture() -> None:
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
async def test_tool_reads_through_the_shared_repository_and_matches_the_fixture() -> None:
    # FastMCP.call_tool's declared return type doesn't reflect that it actually
    # returns a (content_blocks, structured_dict) tuple when structured output
    # is enabled (verified at runtime); silence the resulting stub mismatch.
    content_blocks, structured = await mcp.call_tool(  # type: ignore[misc]
        "strategy_run", {"request": {"strategy_id": "large_model_value_v0"}}
    )
    assert content_blocks  # non-empty text content alongside the structured result
    # JSON-mode validation: the wire payload is JSON-native (lists), not Python tuples.
    report = StrategyRunReport.model_validate_json(json.dumps(structured["result"]))  # type: ignore[index]
    assert report.strategy_id == "large_model_value_v0"
    selected = next(d for d in report.decisions if d.issuer_id == "issuer:adm" and d.cutoff_at.month == 3)
    assert selected.outcome.value == "selected"
    assert str(selected.valuation_gap) == "1.6388"
    assert selected.confidence is not None


@pytest.mark.anyio
async def test_unknown_strategy_returns_structured_unavailable_not_a_crash() -> None:
    _content_blocks, structured = await mcp.call_tool(  # type: ignore[misc]
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
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert sorted(tool.name for tool in tools.tools) == ["strategy_run", "topt_gppe"]

        result = await client.call_tool("strategy_run", {"request": {"strategy_id": "large_model_value_v0"}})
        assert result.isError is not True
        assert result.structuredContent is not None
        report = StrategyRunReport.model_validate_json(json.dumps(result.structuredContent["result"]))
        assert len(report.decisions) == 10
        assert report.golden_mismatches == ()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_default_repository_is_fixture_backed_unless_explicitly_flipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """#361: the shipped default must stay `fixture` until #26 lands a real writer."""
    from llm_service import mcp_server

    monkeypatch.setattr(mcp_server.settings, "strategy_run_backend", "fixture")
    assert isinstance(_default_repository(), FixtureStrategyRunRepository)

    monkeypatch.setattr(mcp_server.settings, "strategy_run_backend", "mart")
    assert isinstance(_default_repository(), PostgresStrategyRunRepository)
