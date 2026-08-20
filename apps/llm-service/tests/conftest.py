"""Test configuration for llm-service.

`MCP_ALLOWED_HOSTS` was set at module import in test_health.py, which mutates
process state for the whole session and makes ordering matter (review). An
autouse fixture scopes it to the tests that need it and leaves the question
"does the deployed service need this configured" where it belongs — in the
deployment, not hidden in a test module's import side effect.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def mcp_allowed_hosts() -> Iterator[None]:
    """FastMCP's DNS-rebinding protection rejects non-localhost Host headers;
    TestClient sends `testserver`."""
    previous = os.environ.get("MCP_ALLOWED_HOSTS")
    os.environ["MCP_ALLOWED_HOSTS"] = '["testserver","localhost"]'
    yield
    if previous is None:
        os.environ.pop("MCP_ALLOWED_HOSTS", None)
    else:
        os.environ["MCP_ALLOWED_HOSTS"] = previous
