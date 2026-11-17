"""Role-scoped PostgreSQL helpers for `llm-service`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from truealpha_runtime.access import AccessContext

from llm_service.config import settings

DatabaseRole = Literal["app_runtime", "mart_readonly", "app_ops_reader"]
_ALLOWED_ROLES: frozenset[str] = frozenset({"app_runtime", "mart_readonly", "app_ops_reader"})


def _connect(*, database_url: str | None = None) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url or settings.database_url,
        connect_timeout=settings.database_connect_timeout_seconds,
        row_factory=dict_row,
    )


def with_role_client[T](
    role: DatabaseRole,
    fn: Callable[[psycopg.Cursor[dict[str, Any]]], T],
    *,
    database_url: str | None = None,
) -> T:
    """Run `fn(cursor)` on a connection holding `role` for this transaction."""
    if role not in _ALLOWED_ROLES:
        raise ValueError(f"unsupported database role: {role}")
    with _connect(database_url=database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set local role {role}")  # noqa: S608 — role checked against closed literal set
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
    with _connect(database_url=database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("set local role app_runtime")
            cur.execute("select set_config('truealpha.tenant_id', %s, true)", (context.tenant_id,))
            cur.execute("select set_config('truealpha.principal_id', %s, true)", (context.principal_id,))
            return fn(cur)
