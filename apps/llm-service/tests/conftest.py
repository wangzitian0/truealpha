# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; from collections.abc import Iterator; import pytest
@pytest.fixture(autouse=True)
def mcp_allowed_hosts() -> Iterator[None]:
    previous = os.environ.get("MCP_ALLOWED_HOSTS"); os.environ["MCP_ALLOWED_HOSTS"] = '["testserver","localhost"]'; yield
    if previous is None: os.environ.pop("MCP_ALLOWED_HOSTS", None)
    else: os.environ["MCP_ALLOWED_HOSTS"] = previous
@pytest.fixture(autouse=True)
def isolate_ambient_database(monkeypatch: pytest.MonkeyPatch) -> None:
    if "DATABASE_URL" not in os.environ: from llm_service.config import settings; monkeypatch.setattr(settings, "database_url", "postgresql://127.0.0.1:1/unreachable")
