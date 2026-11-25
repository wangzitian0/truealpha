"""Shared isolated Postgres + non-superuser `app_service_login` setup for `llm-service` smoke tests."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import bcrypt
import psycopg
import pytest
from llm_service.config import settings
from llm_service.services import auth_service
from mcp.server.fastmcp import FastMCP
from psycopg import sql
from pydantic import SecretStr
from truealpha_runtime.testing import isolated_test_database

ADMIN_EMAIL = "admin@smoke.invalid"
MEMBER_EMAIL = "member@smoke.invalid"
OWNER_B_EMAIL = "ownerb@smoke.invalid"
SMOKE_TENANT_ID = "tenant:truealpha"
OWNER_A_PRINCIPAL_ID = "principal:smoke:member"
OWNER_B_PRINCIPAL_ID = "principal:smoke:owner_b"
ADMIN_PRINCIPAL_ID = "principal:smoke:admin"


@dataclass(frozen=True)
class SmokeDbContext:
    superuser_url: str
    smoke_url: str
    role_name: str
    db_name: str
    password: str


def _with_credentials(url: str, username: str, password: str) -> str:
    parsed = urlsplit(url)
    host_port = parsed.hostname or "127.0.0.1"
    if ":" in host_port:
        host_port = f"[{host_port}]"
    if parsed.port is not None:
        host_port = f"{host_port}:{parsed.port}"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host_port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


@contextmanager
def smoke_app_service_db(monkeypatch: pytest.MonkeyPatch, name_prefix: str = "llm_smoke") -> Iterator[SmokeDbContext]:
    """Provision an isolated migrated Postgres database accessed as a non-superuser in `app_service_login`."""
    with isolated_test_database(name_prefix) as db:
        superuser_url = str(db)
        role_name = f"ta_smoke_{uuid.uuid4().hex[:12]}"
        role_password = secrets.token_hex(16)
        user_password = f"smoke-pass-{secrets.token_hex(12)}"
        pw_hash = bcrypt.hashpw(user_password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("ascii")

        with psycopg.connect(superuser_url, autocommit=True) as admin_conn:
            admin_conn.execute(
                sql.SQL(
                    "create role {} login password {} inherit nosuperuser nocreatedb nocreaterole nobypassrls "
                    "in role app_service_login"
                ).format(sql.Identifier(role_name), sql.Literal(role_password))
            )
            admin_conn.execute(
                sql.SQL("grant connect on database {} to {}").format(sql.Identifier(db.name), sql.Identifier(role_name))
            )
            admin_conn.execute(
                "insert into app.tenants (tenant_id) values (%s) on conflict do nothing", (SMOKE_TENANT_ID,)
            )
            for pid, kind in (
                (auth_service.MCP_OWNER_PRINCIPAL_ID, "administrator"),
                (ADMIN_PRINCIPAL_ID, "administrator"),
                (OWNER_A_PRINCIPAL_ID, "member"),
                (OWNER_B_PRINCIPAL_ID, "member"),
            ):
                admin_conn.execute(
                    "insert into app.principals (principal_id, tenant_id, principal_kind) "
                    "values (%s, %s, %s) on conflict (principal_id) do nothing",
                    (pid, SMOKE_TENANT_ID, kind),
                )
            for pid, email in (
                (ADMIN_PRINCIPAL_ID, ADMIN_EMAIL),
                (OWNER_A_PRINCIPAL_ID, MEMBER_EMAIL),
                (OWNER_B_PRINCIPAL_ID, OWNER_B_EMAIL),
            ):
                admin_conn.execute(
                    "insert into app.principal_credentials (principal_id, email, hashed_password) "
                    "values (%s, %s, %s) on conflict (principal_id) do update "
                    "set email = excluded.email, hashed_password = excluded.hashed_password",
                    (pid, email, pw_hash),
                )

        smoke_url = _with_credentials(superuser_url, role_name, role_password)
        monkeypatch.setattr(settings, "database_url", smoke_url)
        monkeypatch.setattr(settings, "secret_key", SecretStr("smoke-test-secret-key-at-least-32-bytes"))
        auth_service.login_rate_limiter.reset()
        try:
            yield SmokeDbContext(
                superuser_url=superuser_url,
                smoke_url=smoke_url,
                role_name=role_name,
                db_name=db.name,
                password=user_password,
            )
        finally:
            auth_service.login_rate_limiter.reset()
            with psycopg.connect(superuser_url, autocommit=True) as cleanup_conn:
                cleanup_conn.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = %s and usename = %s and pid != pg_backend_pid()",
                    (db.name, role_name),
                )
                cleanup_conn.execute(
                    sql.SQL("revoke all privileges on database {} from {}").format(
                        sql.Identifier(db.name), sql.Identifier(role_name)
                    )
                )
                cleanup_conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role_name)))


def call_mcp_tool(server: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a FastMCP tool synchronously and return its JSON dict payload."""
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if isinstance(result, tuple) and len(result) >= 2:
        content, structured = result[0], result[1]
        if isinstance(structured, dict):
            return structured
        if content and hasattr(content[0], "text"):
            parsed = json.loads(content[0].text)
            if isinstance(parsed, dict):
                return parsed
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        parsed = json.loads(result[0].text)
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError(f"unexpected MCP output for {name}: {result!r}")
