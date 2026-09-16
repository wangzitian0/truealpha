"""One selection rule, two languages (#575).

The Python twin (`strategy_run_postgres.LATEST_RUN_SQL`) is proven against a real
governed head in apps/data-engine/tests/production_topt/test_persistence.py; the
TypeScript twin cannot seed that chain cheaply, so it carries the same statement and
this test pins the two texts. A twin that drifts back to `order by executed_at desc`
alone fails here, not on a visitor's screen.
"""

from __future__ import annotations

import re
from pathlib import Path

from truealpha_contracts.strategy_run_postgres import DECISIONS_FROM_SQL, LATEST_RUN_SQL

REPO = Path(__file__).resolve().parents[3]
TS_TWIN = REPO / "apps" / "app-web" / "src" / "server" / "mart" / "strategy-run-repository.ts"


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.replace("$1", "%s")).strip()


def _ts_latest_run_sql() -> str:
    source = TS_TWIN.read_text()
    match = re.search(r"export const LATEST_RUN_SQL = `(?P<sql>.*?)`;", source, re.S)
    assert match is not None, "the TypeScript twin no longer exports LATEST_RUN_SQL"
    return match.group("sql")


def test_both_twins_rank_the_governed_run_first() -> None:
    assert _normalize(_ts_latest_run_sql()) == _normalize(LATEST_RUN_SQL)


def test_the_rule_is_the_governed_head_not_recency_alone() -> None:
    normalized = _normalize(LATEST_RUN_SQL)
    assert "mart.governed_strategy_run" in normalized
    assert "order by is_governed desc" in normalized


def _ts_decisions_from_sql() -> str:
    source = TS_TWIN.read_text()
    match = re.search(r"const DECISIONS_SQL = `(?P<sql>.*?)`;", source, re.S)
    assert match is not None, "the TypeScript twin no longer declares DECISIONS_SQL"
    sql = match.group("sql")
    start = sql.find("from mart.strategy_decisions d")
    assert start >= 0, "the TypeScript twin no longer reads mart.strategy_decisions"
    return sql[start:]


def test_both_twins_read_a_decision_against_its_own_capture_run() -> None:
    """#877: the decision -> core-result join is the same text in both languages, and it
    is scoped by run. The Python twin's statement is proven against a forced tick in
    apps/data-engine/tests/production_topt/test_degraded_capture_record.py; the
    TypeScript twin carries the same clause, and this pins it."""
    assert _normalize(_ts_decisions_from_sql()) == _normalize(DECISIONS_FROM_SQL)
    normalized = _normalize(DECISIONS_FROM_SQL)
    assert "join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id" in normalized
    assert "t.run_id = scope.capture_run_id" in normalized
