#!/usr/bin/env python3
"""Is this database still the schema the migration chain declares? (#984)

A local database is only safe as a cache if it can tell it has gone stale, and nothing
compared the two. `staging.market_prices_daily` sat in its pre-#939 shape on a developer
machine for days -- 27 `UndefinedColumn` failures in the data-engine suite -- while every
CI job stayed green, because CI's Postgres service declares no volume and starts empty
while a developer's compose Postgres keeps whatever it has ever had. Replay does not
close that gap: the chain carries 113 `alter table` and 162 `drop <object>` statements
against 176 `create ... if not exists` (grep -ohiE over db/migrations/*.sql, 2026-09-23),
so a relation left in a superseded shape keeps it.

How it answers, and why this way:

* The reference is the CHAIN, not a snapshot of it. A committed fingerprint would be a
  second source of truth that must be regenerated whenever a migration lands, and it
  would compare catalogs produced by different server versions. This builds a database
  of its own on the target's own server, applies `db/apply_migrations.sh` to it -- the
  one applier every other call site runs -- and diffs the two catalogs. Same server,
  same version, nothing to keep fresh.
* The TARGET is only ever read. Every statement against it is a `select` from
  `pg_catalog`; the reference database is separate, randomly named, and dropped when the
  run ends.
* Which is still a write, to the SERVER rather than to the target, so building a
  reference on a host that is not this machine is refused. db/local_target.sh decides
  what "this machine" means -- the same file db/reset_database.sh asks, because it makes
  the same kind of write. The two ways forward are `--reference-database-url`, naming a
  database that already ran the chain (nothing is created anywhere, and it is also what
  makes a suite of repeated runs cheap), and TRUEALPHA_ALLOW_REMOTE_REFERENCE=1 for the
  case where the reference has to sit on that server -- same version, same collation --
  to be worth comparing against.

What it does NOT cover, said out loud rather than implied:

* Privileges and ownership. `db/roles.sql` grants are asserted by
  `db/tests/governed_research_access_contract.sql` in ci-db, against real roles; a role
  is a cluster object, so comparing ACLs across two databases reports the environment's
  role inventory rather than the chain's.
* The `dagster` schema's contents. The chain creates the schema; Dagster's own instance
  bootstrap creates the tables in it at run time, so a live database legitimately holds
  relations there that a freshly migrated one does not. The schema's existence is
  compared; what is inside it is not.
* Row data. This is a schema check. `tools/output_invariants.py` is the one that asks
  whether the numbers are possible.

Run:
    python3 tools/schema_drift.py --database-url postgresql://...      # make db-check
Exit codes: 0 no drift, 1 drift found, 2 the question could not be asked.
"""

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

#: "Is this server local?" lives in one file, shared with db/reset_database.sh, which
#: asks the same question about the same kind of write (#990 review). The two keep
#: separate override variables: a scratch database beside staging and `drop database` on
#: staging are not the same decision.
LOCAL_TARGET = Path(__file__).resolve().parents[1] / "db" / "local_target.sh"

#: Set to 1 to build the reference on a server that is not this machine. The other way
#: forward — --reference-database-url — creates nothing anywhere and needs no override.
REMOTE_REFERENCE_ENV = "TRUEALPHA_ALLOW_REMOTE_REFERENCE"

#: Created by Dagster's own instance bootstrap rather than by the chain (see the header).
RUNTIME_OWNED_SCHEMAS = ("dagster",)

#: A reference database that applied the chain has far more relations than this. Fewer
#: means the applier did not run, and a comparison against nothing is green-while-empty:
#: every object in the target would report as "unexpected", or -- worse, if the target is
#: also empty -- the run would report a clean match over two empty catalogs.
MINIMUM_REFERENCE_RELATIONS = 50


@dataclass(frozen=True)
class Category:
    """One kind of catalog object, and how much of each row identifies it.

    `key_width` splits every row into the identity (what the object IS) and the shape
    (what it looks like). That split is what lets the report say "missing", "unexpected"
    and "differs" instead of dumping two lists.
    """

    name: str
    key_width: int
    statement: str


_ALL_SCHEMAS = """
    select n.nspname
      from pg_namespace as n
     where n.nspname not like 'pg\\_%' and n.nspname <> 'information_schema'
     order by 1
"""

CATEGORIES: tuple[Category, ...] = (
    Category(
        "relations",
        2,
        """
        select n.nspname, c.relname, c.relkind
          from pg_class as c
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s)
           and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
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
               a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid) as column_default,
               a.attidentity, a.attgenerated
          from pg_attribute as a
          join pg_class as c on c.oid = a.attrelid
          join pg_namespace as n on n.oid = c.relnamespace
          left join pg_attrdef as d on d.adrelid = a.attrelid and d.adnum = a.attnum
         where n.nspname = any(%(schemas)s)
           and a.attnum > 0 and not a.attisdropped
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
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
         where n.nspname = any(%(schemas)s)
         order by 1, 2, 3
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
         where n.nspname = any(%(schemas)s)
         order by 1, 2, 3
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
         where n.nspname = any(%(schemas)s) and not t.tgisinternal
         order by 1, 2, 3
        """,
    ),
    Category(
        "routines",
        3,
        """
        select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
               p.prokind, md5(p.prosrc)
          from pg_proc as p
          join pg_namespace as n on n.oid = p.pronamespace
         where n.nspname = any(%(schemas)s)
         order by 1, 2, 3
        """,
    ),
    Category(
        "views",
        2,
        """
        select n.nspname, c.relname, pg_get_viewdef(c.oid, true)
          from pg_class as c
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s) and c.relkind in ('v', 'm')
         order by 1, 2
        """,
    ),
    Category(
        "sequences",
        2,
        """
        select n.nspname, c.relname,
               s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcycle
          from pg_sequence as s
          join pg_class as c on c.oid = s.seqrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = any(%(schemas)s)
         order by 1, 2
        """,
    ),
)

Fingerprint = dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]


def _schemas(connection: psycopg.Connection) -> list[str]:
    return [row[0] for row in connection.execute(_ALL_SCHEMAS).fetchall()]


def fingerprint(connection: psycopg.Connection, schemas: Sequence[str]) -> Fingerprint:
    """Every catalog object the chain can declare, keyed by identity."""
    compared = [name for name in schemas if name not in RUNTIME_OWNED_SCHEMAS]
    result: Fingerprint = {}
    for category in CATEGORIES:
        rows = connection.execute(category.statement, {"schemas": compared}).fetchall()
        result[category.name] = {tuple(row[: category.key_width]): tuple(row[category.key_width :]) for row in rows}
    return result


def _shape(shape: tuple[Any, ...]) -> str:
    """`-` for both spellings of "nothing here": a null default and the empty string
    Postgres writes in attidentity/attgenerated read the same to a person."""
    return " | ".join("-" if part is None or part == "" else str(part) for part in shape)


def _render(key: tuple[Any, ...], shape: tuple[Any, ...]) -> str:
    return f"{'.'.join(str(part) for part in key)}  [{_shape(shape)}]"


def differences(live: Fingerprint, declared: Fingerprint) -> list[str]:
    """One line per difference, in the order a reader wants them: what is gone, what is
    there that should not be, what is there but wrong."""
    report: list[str] = []
    for category in CATEGORIES:
        mine = live[category.name]
        theirs = declared[category.name]
        missing = sorted(set(theirs) - set(mine))
        unexpected = sorted(set(mine) - set(theirs))
        changed = sorted(key for key in set(mine) & set(theirs) if mine[key] != theirs[key])
        for key in missing:
            report.append(f"{category.name}: MISSING   {_render(key, theirs[key])}")
        for key in unexpected:
            report.append(f"{category.name}: EXTRA     {_render(key, mine[key])}")
        for key in changed:
            report.append(
                f"{category.name}: DIFFERS   {'.'.join(str(part) for part in key)}\n"
                f"      declared: {_shape(theirs[key])}\n"
                f"      live:     {_shape(mine[key])}"
            )
    return report


def _with_database(conninfo: str, database: str) -> str:
    return make_conninfo(**(conninfo_to_dict(conninfo) | {"dbname": database}))


def target_locality(database_url: str) -> tuple[str, str]:
    """("local"|"remote", redacted url), through db/local_target.sh — the one definition.

    The DSN goes over stdin rather than argv: it carries a password and argv is
    world-readable on Linux.
    """
    completed = subprocess.run(
        ["sh", str(LOCAL_TARGET)],
        input=database_url,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"{LOCAL_TARGET} exited {completed.returncode}")
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    return parsed["ta_locality"], parsed["ta_redacted"]


@contextmanager
def reference_database(server_url: str, *, keep: bool = False) -> Iterator[str]:
    """A database of its own with the declared chain applied, dropped on the way out.

    The guard is here rather than in `main` because this is where the write happens: any
    caller reaching this function is about to CREATE a database on `server_url`'s server
    and DROP it WITH (FORCE) afterwards. "The target is only read" is true and does not
    cover it — the reference is not the target, and short-lived is not the same as
    harmless on a cluster someone else is using (#990 review).
    """
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
        apply_migration_chain(reference_url)
        yield reference_url
    finally:
        if keep:
            print(f"schema_drift: kept the reference database {name}", file=sys.stderr)
        else:
            with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin:
                admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def compare(database_url: str, reference_url: str) -> tuple[list[str], dict[str, int]]:
    """The live catalog against the declared one. Reads both; writes neither."""
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="the database to check (default: $DATABASE_URL). Only read from.",
    )
    parser.add_argument(
        "--reference-database-url",
        default=None,
        help=(
            "an already-migrated database to compare against. Omitted, one is built on the "
            "target's OWN server from db/migrations + db/roles.sql and dropped afterwards — "
            f"which is refused for a non-local server unless {REMOTE_REFERENCE_ENV}=1, because "
            "it creates and force-drops a database there."
        ),
    )
    parser.add_argument(
        "--keep-reference",
        action="store_true",
        help="leave the reference database behind so it can be inspected or reused.",
    )
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
    print(
        "\nReplay (`make db-migrate`) does not repair this: `create ... if not exists` is a no-op "
        "on a relation that already exists in a superseded shape. `make db-reset` recreates the "
        "database from the chain, which is what a fresh CI job starts from.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
