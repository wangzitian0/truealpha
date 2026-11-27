"""Role-scoped PostgreSQL helpers for `llm-service`."""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from truealpha_runtime.access import AccessContext

from llm_service.config import settings

DatabaseRole = Literal["app_runtime", "mart_readonly", "app_ops_reader"]
_ALLOWED_ROLES: frozenset[str] = frozenset({"app_runtime", "mart_readonly", "app_ops_reader"})
_STATEMENT_TIMEOUT = "5s"
_ORIG_PSYCOPG_CONNECT = psycopg.connect
_POOL_LOCK = threading.Lock()
_POOLS: dict[str, ConnectionPool[psycopg.Connection[dict[str, Any]]]] = {}


def _get_pool(dsn: str) -> ConnectionPool[psycopg.Connection[dict[str, Any]]]:
    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            timeout = float(settings.database_connect_timeout_seconds)
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=8,
                timeout=timeout,
                kwargs={
                    "connect_timeout": settings.database_connect_timeout_seconds,
                    "row_factory": dict_row,
                },
                open=True,
            )
            _POOLS[dsn] = pool
        return pool


def close_pools() -> None:
    """Close all open PostgreSQL connection pools cleanly."""
    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


def _connect(*, database_url: str | None = None) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url or settings.database_url,
        connect_timeout=settings.database_connect_timeout_seconds,
        row_factory=dict_row,
    )


@contextmanager
def _role_connection(*, database_url: str | None = None) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    if database_url is not None or psycopg.connect is not _ORIG_PSYCOPG_CONNECT:
        with _connect(database_url=database_url) as conn:
            yield conn
        return
    pool = _get_pool(settings.database_url)
    with pool.connection() as conn, conn.transaction():
        yield conn


def with_role_client[T](
    role: DatabaseRole,
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    """Run `fn(cursor)` on a connection holding `role` for this transaction."""
    if role not in _ALLOWED_ROLES:
        raise ValueError(f"unsupported database role: {role}")
    with _role_connection(database_url=database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set local role {role}")  # noqa: S608 — role checked against closed literal set
            cur.execute("select set_config('statement_timeout', %s, true)", (_STATEMENT_TIMEOUT,))
            return fn(cur)


def with_app_runtime[T](
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    return with_role_client("app_runtime", fn, database_url=database_url)


def with_mart_readonly[T](
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    return with_role_client("mart_readonly", fn, database_url=database_url)


def with_ops_reader[T](
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    return with_role_client("app_ops_reader", fn, database_url=database_url)


def with_owner_scoped_runtime[T](
    context: AccessContext,
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    """Run `fn(cursor)` under `app_runtime` with transaction-local tenant/principal RLS GUCs."""
    with _role_connection(database_url=database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("set local role app_runtime")
            cur.execute("select set_config('statement_timeout', %s, true)", (_STATEMENT_TIMEOUT,))
            cur.execute("select set_config('truealpha.tenant_id', %s, true)", (context.tenant_id,))
            cur.execute("select set_config('truealpha.principal_id', %s, true)", (context.principal_id,))
            return fn(cur)
