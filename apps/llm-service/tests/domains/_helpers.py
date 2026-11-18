"""Shared test helpers for domain-bound API and MCP behavioral tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.services import auth_service

REPO_ROOT = Path(__file__).resolve().parents[4]


def auth_client(
    monkeypatch: pytest.MonkeyPatch, *, principal_kind: str = "member"
) -> tuple[TestClient, dict[str, str]]:
    """Return a TestClient and Bearer headers backed by a stubbed principal."""
    principal = auth_service.PrincipalRecord(
        principal_id=f"principal:{principal_kind}",
        tenant_id="tenant:truealpha",
        principal_kind=principal_kind,
    )
    monkeypatch.setattr(auth_service, "resolve_principal_from_token", lambda *_a, **_k: principal)
    return TestClient(app), {"Authorization": f"Bearer tok-{principal_kind}"}


def load_fixture_strategy_repo() -> Any:
    """Load the deterministic strategy-run fixture repository without duplicating import logic."""
    fixture_path = REPO_ROOT / "common" / "runtime" / "tests" / "fixtures" / "strategy_run_fixture.py"
    spec = importlib.util.spec_from_file_location("strategy_run_fixture", fixture_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FixtureStrategyRunRepository()
