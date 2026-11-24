"""Every data-engine module opens its Postgres connection through `data_engine.db.connect`.

`db.connect` sets `connect_timeout`. A module that calls `psycopg.connect` itself opens a connection
that waits for an unreachable database without a limit. 27 modules did this until this guard existed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import data_engine
import psycopg
import pytest
from data_engine import db
from data_engine.config import settings
from data_engine.sources import gateway

SOURCE_ROOT = Path(data_engine.__file__).resolve().parent
SCRIPT_ROOT = SOURCE_ROOT.parents[1] / "scripts"
ENTRY = SOURCE_ROOT / "db.py"
POOL_MODULES = ("psycopg_pool", "psycopg2", "asyncpg", "sqlalchemy")


def _direct_connects(tree: ast.AST) -> list[int]:
    """Return the line of each call that opens a Postgres connection without `db.connect`."""
    module_names: set[str] = set()
    function_names: set[str] = set()
    class_names: set[str] = set()
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "psycopg":
                    module_names.add(alias.asname or alias.name)
                if alias.name.split(".")[0] in POOL_MODULES:
                    lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in POOL_MODULES:
                lines.append(node.lineno)
            if node.module == "psycopg":
                for alias in node.names:
                    name = alias.asname or alias.name
                    if alias.name == "connect":
                        function_names.add(name)
                    if alias.name in {"Connection", "AsyncConnection"}:
                        class_names.add(name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id in function_names:
            lines.append(node.lineno)
        if not (isinstance(target, ast.Attribute) and target.attr == "connect"):
            continue
        owner = target.value
        if isinstance(owner, ast.Name) and (owner.id in module_names or owner.id in class_names):
            lines.append(node.lineno)
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr in {"Connection", "AsyncConnection"}
            and isinstance(owner.value, ast.Name)
            and owner.value.id in module_names
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def test_only_the_entry_opens_a_postgres_connection() -> None:
    """Fails with the path and line of each module or script that opens a connection without `db.connect`."""
    scanned = [path for root in (SOURCE_ROOT, SCRIPT_ROOT) for path in sorted(root.rglob("*.py")) if path != ENTRY]
    assert len(scanned) > 100, f"the scan reads {len(scanned)} files; it must read the source and the scripts"
    assert any(path.parent == SCRIPT_ROOT for path in scanned), f"{SCRIPT_ROOT} holds no script"
    offenders = [
        f"{path.name}:{line}"
        for path in scanned
        for line in _direct_connects(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]
    assert offenders == [], f"open the connection with data_engine.db.connect: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\npsycopg.connect(url)\n",
        "import psycopg as pg\npg.connect(url, autocommit=True)\n",
        "from psycopg import connect\nconnect(url)\n",
        "from psycopg import connect as open_database\nopen_database(url)\n",
        "import psycopg\npsycopg.Connection.connect(url)\n",
        "from psycopg import AsyncConnection\nAsyncConnection.connect(url)\n",
        "from psycopg_pool import ConnectionPool\n",
    ],
)
def test_the_scan_sees_each_way_to_open_a_connection(source: str) -> None:
    """Fails when the scan misses one spelling of a direct connection."""
    assert _direct_connects(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        "from data_engine import db\ndb.connect()\n",
        "import psycopg\nrow = psycopg.sql.SQL('select 1')\n",
        "import socket\nsocket.connect(address)\n",
    ],
)
def test_the_scan_leaves_calls_that_are_not_connections(source: str) -> None:
    """Fails when the scan flags the entry, a psycopg helper or a connect call of another library."""
    assert _direct_connects(ast.parse(source)) == []


def test_the_entry_sets_the_connect_timeout_and_the_autocommit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails when `db.connect` drops the timeout, the database URL or the autocommit flag."""
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record(*args: object, **kwargs: object) -> str:
        seen.append((args, kwargs))
        return "connection"

    monkeypatch.setattr(psycopg, "connect", record)

    assert db.connect() == "connection"
    assert db.connect(autocommit=True) == "connection"

    expected = {"connect_timeout": settings.database_connect_timeout_seconds}
    assert seen == [
        ((settings.database_url,), {**expected, "autocommit": False}),
        ((settings.database_url,), {**expected, "autocommit": True}),
    ]


def test_the_external_call_ledger_connection_uses_the_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails when the ledger connection of the source gateway skips the connect timeout."""
    seen: list[dict[str, object]] = []

    class _Connection:
        closed = False

    def record(*_args: object, **kwargs: object) -> _Connection:
        seen.append(kwargs)
        return _Connection()

    monkeypatch.setattr(psycopg, "connect", record)
    monkeypatch.setattr(gateway, "_pg_conn", None)

    gateway._pg()

    assert seen == [{"connect_timeout": settings.database_connect_timeout_seconds, "autocommit": True}]
