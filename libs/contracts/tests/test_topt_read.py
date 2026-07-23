"""Unit coverage for `PostgresToptGppeRepository.latest()` via a monkeypatched
`psycopg.connect` (no live DB): the class opens its own connection per call, and
exercising the `mart.current_pointer` advance path against a real Postgres would
mean inserting an append-only, undeletable 'production' pointer row (#378's
`reject_mutation` trigger forbids update/delete) into the shared local dev
database — this mirrors the App-side `topt-gppe-repository.test.ts` fake-client
approach instead, which the TS test's own docstring justifies the same way.

Regression target: #434's follow-up sync surfaced that the primary
`mart.current_pointer_head` query selected `target_run_id` (unaliased) while the
repository reads the row back as `head["run_id"]` under `dict_row` — a `KeyError`
that only fires once `mart.current_pointer` actually has a row for
('production', 'gross_profit_per_employee'), i.e. exactly when #378/#427's
evidence-graph wiring starts advancing it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from truealpha_contracts import topt_read as topt_read_module
from truealpha_contracts.topt_read import PostgresToptGppeRepository

RUN_ID = "capture-run:" + "b" * 64


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConnection:
    def __init__(self, responder: Callable[[str, Any], list[dict[str, Any]]]) -> None:
        self._responder = responder

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return _FakeCursor(self._responder(sql, params))

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _install_fake_connect(monkeypatch: Any, responder: Callable[[str, Any], list[dict[str, Any]]]) -> None:
    monkeypatch.setattr(topt_read_module.psycopg, "connect", lambda *a, **kw: _FakeConnection(responder))


def _row_keyed_like_postgres_would(sql: str, value: str) -> dict[str, Any]:
    """`dict_row` keys a returned row by the SELECTed column's name or `AS` alias —
    never by what the caller later reads it as. Deriving the key from the SQL text
    itself (rather than hardcoding "run_id") is what makes this test able to catch
    a missing/wrong alias instead of just re-asserting the code under test."""
    column_expr = sql.lower().split("select", 1)[1].split("from", 1)[0].strip()
    key = column_expr.split(" as ")[1].strip() if " as " in column_expr else column_expr
    return {key: value}


def test_latest_resolves_head_from_current_pointer_before_the_acceptance_fallback(monkeypatch: Any) -> None:
    calls: list[str] = []

    def responder(sql: str, _params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        if "current_pointer_head" in sql:
            return [_row_keyed_like_postgres_would(sql, RUN_ID)]
        if "topt_capture_status" in sql:
            raise AssertionError("must not fall back once the pointer resolves")
        if "topt_gppe_results" in sql:
            return [
                {"listing_id": "listing:aaa", "availability": "available", "gppe": "1500000.00", "confidence": "0.90"}
            ]
        if "datahub_quality_report" in sql:
            return [{"payload": {"independent_reconciliation": "0.25"}}]
        raise AssertionError(f"unexpected query: {sql}")

    _install_fake_connect(monkeypatch, responder)
    repo = PostgresToptGppeRepository(database_url="postgresql://unused/unused")

    report = repo.latest()

    assert report.run_id == RUN_ID
    assert report.cells[0].gppe == "1500000.00"
    assert report.quality == {"independent_reconciliation": "0.25"}
    assert any("current_pointer_head" in sql for sql in calls)


def test_latest_falls_back_to_acceptance_gated_join_when_pointer_is_empty(monkeypatch: Any) -> None:
    def responder(sql: str, _params: Any) -> list[dict[str, Any]]:
        if "current_pointer_head" in sql:
            return []
        if "topt_capture_status" in sql:
            return [{"run_id": RUN_ID}]
        if "topt_gppe_results" in sql:
            return []
        if "datahub_quality_report" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    _install_fake_connect(monkeypatch, responder)
    repo = PostgresToptGppeRepository(database_url="postgresql://unused/unused")

    report = repo.latest()

    assert report.run_id == RUN_ID
    assert report.cells == ()
    assert report.quality is None


def test_latest_reports_unavailable_when_neither_source_resolves(monkeypatch: Any) -> None:
    def responder(sql: str, _params: Any) -> list[dict[str, Any]]:
        if "current_pointer_head" in sql or "topt_capture_status" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    _install_fake_connect(monkeypatch, responder)
    repo = PostgresToptGppeRepository(database_url="postgresql://unused/unused")

    result = repo.latest()

    assert result.reason == "no accepted (quality-reported) production TOPT run"


def test_latest_fails_closed_instead_of_raising_on_a_malformed_head_row(monkeypatch: Any) -> None:
    """truealpha#462 AC2: a row-shape mismatch (the exact class of bug #461 was --
    the query selected one column name and the code read another) must degrade to
    ToptGppeUnavailable(reason="schema_mismatch"), not crash the whole MCP tool
    call. Simulated here the same way #461 actually happened: the primary query's
    row is missing the key the code reads."""

    def responder(sql: str, _params: Any) -> list[dict[str, Any]]:
        if "current_pointer_head" in sql:
            # No "as run_id" alias applied -- the row is keyed by the raw column
            # expression, so `head["run_id"]` raises KeyError, exactly like #461.
            return [{"target_run_id": RUN_ID}]
        raise AssertionError(f"unexpected query: {sql}")

    _install_fake_connect(monkeypatch, responder)
    repo = PostgresToptGppeRepository(database_url="postgresql://unused/unused")

    result = repo.latest()

    assert result.reason == "schema_mismatch"


def test_latest_fails_closed_on_a_database_error(monkeypatch: Any) -> None:
    def raise_connect(*_a: Any, **_kw: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(topt_read_module.psycopg, "connect", raise_connect)
    repo = PostgresToptGppeRepository(database_url="postgresql://unused/unused")

    result = repo.latest()

    assert result.reason == "database_unavailable"
