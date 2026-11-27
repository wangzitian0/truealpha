"""Tests for role-scoped PostgreSQL connection pooling in `llm_service.services._db`."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from llm_service.services import _db


class _FakeTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> _FakeTransaction:
        self._events.append("tx_enter")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._events.append("tx_exit")


class _FakeCursor:
    def __init__(self, statements: list[tuple[str, Any]]) -> None:
        self._statements = statements

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._statements.append((sql, params))


class _FakePooledConnection:
    def __init__(self, events: list[str], statements: list[tuple[str, Any]]) -> None:
        self._events = events
        self._statements = statements

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._events)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._statements)


def test_role_client_uses_and_closes_lazy_pool_per_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    statements: list[tuple[str, Any]] = []
    created_dsns: list[str] = []

    class _FakePool:
        def __init__(self, dsn: str, **_kwargs: Any) -> None:
            created_dsns.append(dsn)
            self.closed = False

        @contextmanager
        def connection(self):
            events.append("pool_borrow")
            try:
                yield _FakePooledConnection(events, statements)
            finally:
                events.append("pool_return")

        def close(self) -> None:
            self.closed = True
            events.append("pool_close")

    monkeypatch.setattr(_db, "ConnectionPool", _FakePool)
    monkeypatch.setattr(_db.settings, "database_url", "postgresql://runtime@db:55432/truealpha")
    _db.close_pools()
    events.clear()

    r1 = _db.with_mart_readonly(lambda _cur: "ok-1")
    r2 = _db.with_mart_readonly(lambda _cur: "ok-2")

    assert (r1, r2) == ("ok-1", "ok-2")
    assert created_dsns == ["postgresql://runtime@db:55432/truealpha"]
    assert events == [
        "pool_borrow",
        "tx_enter",
        "tx_exit",
        "pool_return",
        "pool_borrow",
        "tx_enter",
        "tx_exit",
        "pool_return",
    ]
    assert statements[0][0] == "set local role mart_readonly"

    _db.close_pools()
    assert events[-1] == "pool_close"


def test_explicit_database_url_bypasses_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[str] = []

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def cursor(self) -> _FakeCursor:
            return _FakeCursor([])

    def _fake_connect(dsn: str, **_kwargs: Any) -> _FakeConn:
        connected.append(dsn)
        return _FakeConn()

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    monkeypatch.setattr(_db, "_get_pool", lambda _dsn: pytest.fail("should not use pool when monkeypatched"))

    res = _db.with_ops_reader(lambda _cur: "direct")
    assert res == "direct"
    assert len(connected) == 1
