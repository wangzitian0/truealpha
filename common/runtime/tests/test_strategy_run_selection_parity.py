"""One selection rule in Python SSOT (#575, #877, #1716): the TypeScript SQL twin is deleted."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from truealpha_runtime.strategy_run_postgres import DECISIONS_FROM_SQL, LATEST_RUN_SQL

pytestmark = pytest.mark.contract

REPO = Path(__file__).resolve().parents[3]
TS_TWIN = REPO / "apps" / "app-web" / "src" / "server" / "mart" / "strategy-run-repository.ts"


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_ts_strategy_run_twin_is_deleted() -> None:
    assert not TS_TWIN.exists(), "apps/app-web must not reintroduce direct Postgres strategy-run queries"


def test_the_rule_is_the_governed_head_not_recency_alone() -> None:
    normalized = _normalize(LATEST_RUN_SQL)
    assert "mart.served_head" in normalized
    assert (
        "exists ( select 1 from head_scope hs join mart.strategy_run_capture b "
        "on b.capture_run_id = hs.run_id where hs.run_id is not null and b.strategy_run_id = r.strategy_run_id )"
        in normalized
    )


def test_decisions_read_scopes_each_decision_by_capture_run() -> None:
    normalized = _normalize(DECISIONS_FROM_SQL)
    assert "join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id" in normalized
    assert "m.run_id = scope.capture_run_id" in normalized
