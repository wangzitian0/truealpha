"""#1235 criterion 1, MCP half: the `strategy_run` tool names the definition of the served run.

Production serves runs of definition 0.1.0, whose tier bands no one reviewed. Staging serves
the reviewed 1.0.0. The tool response must say which one a caller reads.

The test drives the deployed server (`build_mcp_server()` with its default Postgres
repository) against a real Postgres. It seeds inside one transaction that it rolls back, and
redirects `psycopg.connect` onto that transaction: the seam of test_mcp_identity_leak.py.
The cases come from the definition registry, so a future definition file is covered with no edit.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import psycopg
import pytest
from llm_service.mcp_server import build_mcp_server
from truealpha_contracts.strategy import StrategyDefinitionIdentity, strategy_definition_registry
from truealpha_contracts.strategy_run import StrategyRunReport

_REGISTRY = strategy_definition_registry()
_REVIEWED = next(entry for entry in _REGISTRY if not entry.provisional_semantics)
#: Later than every run that another test commits, so the seeded run is the newest one.
_EXECUTED_AT = "2028-01-01T00:00:00Z"


class _BorrowedConnection:
    """Gives the repository the test's own connection; commit and close stay with the test."""

    def __init__(self, real: psycopg.Connection[Any]) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **kwargs: Any) -> Any:
        return self._real.cursor(**kwargs)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail

    url = os.environ.get("DATABASE_URL")
    if not url:
        skip_or_fail("DATABASE_URL must be explicitly provided for MCP tests (no default to port 5432)")
        return
    try:
        connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: _BorrowedConnection(connection))
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


async def _served_result(connection: psycopg.Connection[Any], *, version: str, content_sha256: str) -> dict[str, Any]:
    """Record the newest run with this definition identity and call the tool as a client does."""
    run_id = "strategy-run:" + secrets.token_hex(32)
    connection.execute(
        """
        insert into mart.strategy_runs
          (strategy_run_id, content_sha256, strategy_key, strategy_version,
           definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
        values (%s, %s, 'large_model_value_v0', %s, %s, %s, 'preview', %s)
        """,
        (run_id, secrets.token_hex(32), version, content_sha256, secrets.token_hex(32), _EXECUTED_AT),
    )
    connection.execute(
        """
        insert into mart.strategy_decisions
          (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at, eligible, outcome)
        values (%s, %s, %s, 'issuer:mcp-1235', timestamptz '2026-06-30T23:59:59Z', true, 'selected')
        """,
        ("strategy-decision:" + secrets.token_hex(32), secrets.token_hex(32), run_id),
    )
    _content, structured = await build_mcp_server().call_tool(  # type: ignore[misc]
        "strategy_run", {"request": {"strategy_id": "large_model_value_v0"}}
    )
    result: dict[str, Any] = structured["result"]  # type: ignore[index]
    assert result.get("strategy_run_id") == run_id, f"the tool served {result!r}, not the seeded run"
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("registered", _REGISTRY, ids=lambda entry: f"definition-{entry.version}")
async def test_the_strategy_run_tool_names_the_definition_and_its_review_state(
    database: psycopg.Connection[Any], registered: StrategyDefinitionIdentity
) -> None:
    result = await _served_result(database, version=registered.version, content_sha256=registered.content_sha256)

    expected_state = "provisional" if registered.provisional_semantics else "reviewed"
    assert result.get("definition") == {
        "version": registered.version,
        "content_sha256": registered.content_sha256,
        "review_state": expected_state,
    }
    # The wire payload is the shared contract, marker included.
    report = StrategyRunReport.model_validate_json(json.dumps(result))
    assert report.definition is not None
    assert report.definition.review_state == expected_state


@pytest.mark.anyio
async def test_the_strategy_run_tool_calls_a_definition_outside_the_registry_unknown(
    database: psycopg.Connection[Any],
) -> None:
    result = await _served_result(database, version=_REVIEWED.version, content_sha256="f" * 64)

    assert result.get("definition") == {
        "version": _REVIEWED.version,
        "content_sha256": "f" * 64,
        "review_state": "unknown",
    }
