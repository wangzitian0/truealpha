#!/usr/bin/env python3
"""Compare live Postgres catalog against migration-chain reference database (#984)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.checks import redact_postgres_exception
from truealpha_runtime.testing import DB_DIR, apply_migration_chain

log = logging.getLogger(__name__)

REFERENCE_PREFIX = "truealpha_chain_reference_"
REFERENCE_NAME = re.compile(rf"{re.escape(REFERENCE_PREFIX)}(\d{{10}})_\d+_[0-9a-f]{{8}}", re.ASCII)
STALE_REFERENCE_AGE = timedelta(hours=1)
SWEEP_STATEMENT_TIMEOUT = "60s"
LOCAL_TARGET = Path(__file__).resolve().parents[4] / "db" / "local_target.sh"
REMOTE_REFERENCE_ENV = "TRUEALPHA_ALLOW_REMOTE_REFERENCE"
RUNTIME_OWNED_SCHEMAS = ("dagster",)
MINIMUM_REFERENCE_RELATIONS = 50


@dataclass(frozen=True)
class Category:
    name: str
    key_width: int
    statement: str


_ALL_SCHEMAS = "select n.nspname from pg_namespace as n where n.nspname not like 'pg\\_%' and n.nspname <> 'information_schema' order by 1"

_NS_C = "join pg_namespace as n on n.oid = c.relnamespace "
_W = "where n.nspname = any(%(schemas)s)"

CATEGORIES: tuple[Category, ...] = (
    Category(
        "relations",
        2,
        f"select n.nspname, c.relname, c.relkind from pg_class as c {_NS_C}"
        f"{_W} and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S') order by 1, 2",
    ),
    Category(
        "columns",
        3,
        "select n.nspname, c.relname, a.attname, "
        "row_number() over (partition by a.attrelid order by a.attnum) as position, "
        "format_type(a.atttypid, a.atttypmod) as type, a.attnotnull, pg_get_expr(d.adbin, d.adrelid) as column_default, "
        "a.attidentity, a.attgenerated from pg_attribute as a "
        f"join pg_class as c on c.oid = a.attrelid {_NS_C}"
        "left join pg_attrdef as d on d.adrelid = a.attrelid and d.adnum = a.attnum "
        f"{_W} and a.attnum > 0 and not a.attisdropped and c.relkind in ('r', 'p', 'v', 'm', 'f') order by 1, 2, 3",
    ),
    Category(
        "constraints",
        3,
        f"select n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid) from pg_constraint as con "
        f"join pg_class as c on c.oid = con.conrelid {_NS_C}{_W} order by 1, 2, 3",
    ),
    Category(
        "indexes",
        3,
        "select n.nspname, c.relname, i.relname, pg_get_indexdef(x.indexrelid) from pg_index as x "
        f"join pg_class as i on i.oid = x.indexrelid join pg_class as c on c.oid = x.indrelid {_NS_C}{_W} order by 1, 2, 3",
    ),
    Category(
        "triggers",
        3,
        "select n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid) from pg_trigger as t "
        f"join pg_class as c on c.oid = t.tgrelid {_NS_C}{_W} and not t.tgisinternal order by 1, 2, 3",
    ),
    Category(
        "routines",
        3,
        "select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), p.prokind, md5(p.prosrc) "
        f"from pg_proc as p join pg_namespace as n on n.oid = p.pronamespace {_W} order by 1, 2, 3",
    ),
    Category(
        "views",
        2,
        f"select n.nspname, c.relname, pg_get_viewdef(c.oid, true) from pg_class as c {_NS_C}"
        f"{_W} and c.relkind in ('v', 'm') order by 1, 2",
    ),
    Category(
        "sequences",
        2,
        "select n.nspname, c.relname, s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcycle from pg_sequence as s "
        f"join pg_class as c on c.oid = s.seqrelid {_NS_C}{_W} order by 1, 2",
    ),
    Category(
        "rls",
        2,
        f"select n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity from pg_class as c {_NS_C}"
        f"{_W} and c.relkind in ('r', 'p') order by 1, 2",
    ),
    Category(
        "policies",
        3,
        "select schemaname, tablename, policyname, permissive, roles::text, cmd, qual, with_check "
        "from pg_policies where schemaname = any(%(schemas)s) order by 1, 2, 3",
    ),
)

Fingerprint = dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]


def _schemas(connection: psycopg.Connection) -> list[str]:
    return [row[0] for row in connection.execute(_ALL_SCHEMAS).fetchall()]


def fingerprint(connection: psycopg.Connection, schemas: Sequence[str]) -> Fingerprint:
    compared = [name for name in schemas if name not in RUNTIME_OWNED_SCHEMAS]
    return {
        category.name: {
            tuple(row[: category.key_width]): tuple(row[category.key_width :])
            for row in connection.execute(category.statement, {"schemas": compared}).fetchall()
        }
        for category in CATEGORIES
    }


def _shape(shape: tuple[Any, ...]) -> str:
    return " | ".join("-" if part is None or part == "" else str(part) for part in shape)


def _render(key: tuple[Any, ...], shape: tuple[Any, ...]) -> str:
    return f"{'.'.join(str(part) for part in key)}  [{_shape(shape)}]"


def differences(live: Fingerprint, declared: Fingerprint) -> list[str]:
    report: list[str] = []
    for category in CATEGORIES:
        mine, theirs = live[category.name], declared[category.name]
        report.extend(
            f"{category.name}: MISSING   {_render(key, theirs[key])}" for key in sorted(set(theirs) - set(mine))
        )
        report.extend(
            f"{category.name}: EXTRA     {_render(key, mine[key])}" for key in sorted(set(mine) - set(theirs))
        )
        for key in sorted(set(mine) & set(theirs)):
            if mine[key] != theirs[key]:
                report.append(
                    f"{category.name}: DIFFERS   {'.'.join(str(part) for part in key)}\n"
                    f"      declared: {_shape(theirs[key])}\n"
                    f"      live:     {_shape(mine[key])}"
                )
    return report


def _with_database(conninfo: str, database: str) -> str:
    params: dict[str, Any] = conninfo_to_dict(conninfo) | {"dbname": database}
    return make_conninfo(**params)


def target_locality(database_url: str) -> tuple[str, str]:
    completed = subprocess.run(
        ["sh", str(LOCAL_TARGET)], input=database_url, capture_output=True, text=True, check=False, timeout=30
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"{LOCAL_TARGET} exited {completed.returncode}")
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    return parsed["ta_locality"], parsed["ta_redacted"]


ROLES_STUB = "-- Left out on purpose: roles are state of the whole server, not of the scratch database.\n"


@contextmanager
def _chain_without_roles(db_dir: Path) -> Iterator[Path]:
    """Create temporary chain directory using migrations from ``db_dir`` and an empty ``roles.sql``."""
    with tempfile.TemporaryDirectory(prefix="truealpha_chain_without_roles_") as scratch:
        root = Path(scratch)
        for name in ("migrations", "apply_migrations.sh"):
            (root / name).symlink_to(db_dir / name)
        (root / "roles.sql").write_text(ROLES_STUB)
        yield root


@contextmanager
def reference_database(
    server_url: str,
    *,
    keep: bool = False,
    db_dir: Path | None = None,
    allow_remote: bool = False,
    skip_roles: bool = False,
) -> Iterator[str]:
    """Yield temporary reference database URL on `server_url` and drop on exit."""
    if not allow_remote:
        locality, redacted = target_locality(server_url)
        if locality != "local" and os.environ.get(REMOTE_REFERENCE_ENV) != "1":
            raise RuntimeError(
                f"{redacted} is not a local server, and building a reference there means CREATE "
                f"DATABASE followed by DROP DATABASE ... WITH (FORCE) on it. Two ways forward: pass "
                f"--reference-database-url naming a database that already ran the chain (nothing is "
                f"created anywhere, and the target stays read-only), or set {REMOTE_REFERENCE_ENV}=1 "
                f"to build the reference on that server anyway."
            )
    admin_url = _with_database(server_url, "postgres")
    name = f"{REFERENCE_PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        reference_url = _with_database(server_url, name)
        chain_dir = DB_DIR if db_dir is None else db_dir
        if skip_roles:
            with _chain_without_roles(chain_dir) as chain_without_roles:
                apply_migration_chain(reference_url, db_dir=chain_without_roles)
        elif db_dir is None:
            apply_migration_chain(reference_url)
        else:
            apply_migration_chain(reference_url, db_dir=db_dir)
        yield reference_url
    finally:
        if keep:
            print(
                f"schema_drift: kept the reference database {name}. The deployed check of that server drops "
                f"a scratch reference that is older than {STALE_REFERENCE_AGE.total_seconds() / 3600:g} h.",
                file=sys.stderr,
            )
        else:
            with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin:
                admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def drop_stale_references(
    server_url: str, *, older_than: timedelta = STALE_REFERENCE_AGE, now: float | None = None
) -> list[str]:
    """Drop stale scratch reference databases matching ``REFERENCE_NAME``."""
    moment = time.time() if now is None else now
    dropped: list[str] = []
    with psycopg.connect(
        _with_database(server_url, "postgres"),
        connect_timeout=10,
        autocommit=True,
        options=f"-c statement_timeout={SWEEP_STATEMENT_TIMEOUT}",
    ) as admin:
        rows = admin.execute(
            "select datname from pg_database where datname like %s order by datname", (f"{REFERENCE_PREFIX}%",)
        ).fetchall()
        for (name,) in rows:
            match = REFERENCE_NAME.fullmatch(name)
            if match is None or moment - int(match.group(1)) < older_than.total_seconds():
                continue
            try:
                admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
            except psycopg.Error:
                log.warning("could not drop the stale reference database %s", name, exc_info=True)
                continue
            log.warning("dropped the stale reference database %s", name)
            dropped.append(name)
    return dropped


def compare(database_url: str, reference_url: str) -> tuple[list[str], dict[str, int]]:
    with psycopg.connect(reference_url, connect_timeout=10) as reference:
        declared_schemas = _schemas(reference)
        declared = fingerprint(reference, declared_schemas)
    if len(declared["relations"]) < MINIMUM_REFERENCE_RELATIONS:
        raise RuntimeError(
            f"the reference database declares only {len(declared['relations'])} relations "
            f"(expected at least {MINIMUM_REFERENCE_RELATIONS}) — the migration chain did not "
            f"reach it, so there is nothing to compare against and a clean report would mean nothing"
        )
    with psycopg.connect(database_url, connect_timeout=10) as live:
        live_schemas = _schemas(live)
        observed = fingerprint(live, declared_schemas)

    report = [f"schemas: MISSING   {name}" for name in sorted(set(declared_schemas) - set(live_schemas))] + [
        f"schemas: EXTRA     {name}" for name in sorted(set(live_schemas) - set(declared_schemas))
    ]
    report += differences(observed, declared)
    counts = {"schemas": len(declared_schemas)} | {name: len(declared[name]) for name in declared}
    return report, counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--reference-database-url", default=None)
    parser.add_argument("--keep-reference", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.database_url:
        parser.error("--database-url (or DATABASE_URL) is required")
    try:
        if arguments.reference_database_url:
            report, counts = compare(arguments.database_url, arguments.reference_database_url)
        else:
            with reference_database(arguments.database_url, keep=arguments.keep_reference) as reference_url:
                report, counts = compare(arguments.database_url, reference_url)
    except (psycopg.Error, RuntimeError, AssertionError) as error:
        message = redact_postgres_exception(error, database_url=arguments.database_url, connect_timeout_seconds=10)
        if arguments.reference_database_url:
            message = redact_postgres_exception(
                RuntimeError(message), database_url=arguments.reference_database_url, connect_timeout_seconds=10
            ).removeprefix("RuntimeError: ")
        print(f"schema_drift: cannot compare: {message}", file=sys.stderr)
        return 2

    measured = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    if not report:
        print(f"schema drift: none. The chain declares {measured}; the database matches all of it.")
        return 0
    print(f"schema drift: {len(report)} difference(s) against the declared chain.", file=sys.stderr)
    print(f"The chain declares {measured}.", file=sys.stderr)
    for line in report:
        print(f"  {line}", file=sys.stderr)
    print("\nRun `make db-reset` to recreate the database from the chain.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
