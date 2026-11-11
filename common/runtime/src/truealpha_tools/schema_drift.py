#!/usr/bin/env python3
"""Compare a live Postgres catalog against a fresh migration-chain reference database (#984)."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.testing import apply_migration_chain

LOCAL_TARGET = Path(__file__).resolve().parents[4] / "db" / "local_target.sh"
REMOTE_REFERENCE_ENV = "TRUEALPHA_ALLOW_REMOTE_REFERENCE"
RUNTIME_OWNED_SCHEMAS = ("dagster",)
MINIMUM_REFERENCE_RELATIONS = 50


@dataclass(frozen=True)
class Category:
    name: str
    key_width: int
    statement: str


_ALL_SCHEMAS = """
    select n.nspname from pg_namespace as n
    where n.nspname not like 'pg\\_%' and n.nspname <> 'information_schema'
    order by 1
"""

CATEGORIES: tuple[Category, ...] = (
    Category(
        "relations",
        2,
        """
        select n.nspname, c.relname, c.relkind
          from pg_class as c join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
         order by 1, 2
        """,
    ),
    Category(
        "columns",
        3,
        """
        select n.nspname, c.relname, a.attname,
               row_number() over (partition by a.attrelid order by a.attnum) as position,
               format_type(a.atttypid, a.atttypmod) as type,
               a.attnotnull, pg_get_expr(d.adbin, d.adrelid) as column_default,
               a.attidentity, a.attgenerated
          from pg_attribute as a
          join pg_class as c on c.oid = a.attrelid
          join pg_namespace as n on n.oid = c.relnamespace
          left join pg_attrdef as d on d.adrelid = a.attrelid and d.adnum = a.attnum
         where n.nspname = any(%(schemas)s)
           and a.attnum > 0 and not a.attisdropped and c.relkind in ('r', 'p', 'v', 'm', 'f')
         order by 1, 2, 3
        """,
    ),
    Category(
        "constraints",
        3,
        """
        select n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid)
          from pg_constraint as con
          join pg_class as c on c.oid = con.conrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) order by 1, 2, 3
        """,
    ),
    Category(
        "indexes",
        3,
        """
        select n.nspname, c.relname, i.relname, pg_get_indexdef(x.indexrelid)
          from pg_index as x
          join pg_class as i on i.oid = x.indexrelid
          join pg_class as c on c.oid = x.indrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) order by 1, 2, 3
        """,
    ),
    Category(
        "triggers",
        3,
        """
        select n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
          from pg_trigger as t
          join pg_class as c on c.oid = t.tgrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) and not t.tgisinternal order by 1, 2, 3
        """,
    ),
    Category(
        "routines",
        3,
        """
        select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), p.prokind, md5(p.prosrc)
          from pg_proc as p join pg_namespace as n on n.oid = p.pronamespace
         where n.nspname = any(%(schemas)s) order by 1, 2, 3
        """,
    ),
    Category(
        "views",
        2,
        """
        select n.nspname, c.relname, pg_get_viewdef(c.oid, true)
          from pg_class as c join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) and c.relkind in ('v', 'm') order by 1, 2
        """,
    ),
    Category(
        "sequences",
        2,
        """
        select n.nspname, c.relname, s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcycle
          from pg_sequence as s
          join pg_class as c on c.oid = s.seqrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) order by 1, 2
        """,
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
        for key in sorted(set(theirs) - set(mine)):
            report.append(f"{category.name}: MISSING   {_render(key, theirs[key])}")
        for key in sorted(set(mine) - set(theirs)):
            report.append(f"{category.name}: EXTRA     {_render(key, mine[key])}")
        for key in sorted(k for k in set(mine) & set(theirs) if mine[k] != theirs[k]):
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


@contextmanager
def reference_database(
    server_url: str, *, keep: bool = False, db_dir: Path | None = None, allow_remote: bool = False
) -> Iterator[str]:
    """A database of its own on `server_url` that ran the chain; dropped when the block ends.

    `db_dir` names the directory that holds `migrations/`, `roles.sql` and `apply_migrations.sh` when it is not
    the checkout of this file: the data-engine image bakes it at `/app/db`. `allow_remote` is the in-code form
    of `REMOTE_REFERENCE_ENV`. The caller states that the reference has to sit on this server. The deployed check
    of an environment passes it for the database that it runs against, never for a URL that a person supplied."""
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
    name = f"truealpha_chain_reference_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        reference_url = _with_database(server_url, name)
        if db_dir is None:
            apply_migration_chain(reference_url)
        else:
            apply_migration_chain(reference_url, db_dir=db_dir)
        yield reference_url
    finally:
        if keep:
            print(f"schema_drift: kept the reference database {name}", file=sys.stderr)
        else:
            with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin:
                admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


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
        print(f"schema_drift: cannot compare: {error}", file=sys.stderr)
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
