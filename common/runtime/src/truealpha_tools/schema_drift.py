# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import os; import shlex; import subprocess; import sys; import uuid; from collections.abc import Iterator, Sequence; from contextlib import contextmanager; from dataclasses import dataclass; from pathlib import Path; from typing import Any; import psycopg; from psycopg import sql; from psycopg.conninfo import conninfo_to_dict, make_conninfo; from truealpha_runtime.testing import apply_migration_chain; LOCAL_TARGET = Path(__file__).resolve().parents[4] / "db" / "local_target.sh"; REMOTE_REFERENCE_ENV = "TRUEALPHA_ALLOW_REMOTE_REFERENCE"; RUNTIME_OWNED_SCHEMAS = ("dagster",); MINIMUM_REFERENCE_RELATIONS = 50
@dataclass(frozen=True)
class Category: name: str; key_width: int; statement: str
_ALL_SCHEMAS = "\n    select n.nspname\n      from pg_namespace as n\n     where n.nspname not like 'pg\\_%' and n.nspname <> 'information_schema'\n     order by 1\n"; CATEGORIES: tuple[Category, ...] = ( Category( "relations", 2, "\n        select n.nspname, c.relname, c.relkind\n          from pg_class as c\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s)\n           and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')\n         order by 1, 2\n        ", ), Category( "columns", 3, "\n        select n.nspname, c.relname, a.attname,\n               row_number() over (partition by a.attrelid order by a.attnum) as position,\n               format_type(a.atttypid, a.atttypmod) as type,\n               a.attnotnull,\n               pg_get_expr(d.adbin, d.adrelid) as column_default,\n               a.attidentity, a.attgenerated\n          from pg_attribute as a\n          join pg_class as c on c.oid = a.attrelid\n          join pg_namespace as n on n.oid = c.relnamespace\n          left join pg_attrdef as d on d.adrelid = a.attrelid and d.adnum = a.attnum\n         where n.nspname = any(%(schemas)s)\n           and a.attnum > 0 and not a.attisdropped\n           and c.relkind in ('r', 'p', 'v', 'm', 'f')\n         order by 1, 2, 3\n        ", ), Category( "constraints", 3, '\n        select n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid)\n          from pg_constraint as con\n          join pg_class as c on c.oid = con.conrelid\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s)\n         order by 1, 2, 3\n        ', ), Category( "indexes", 3, '\n        select n.nspname, c.relname, i.relname, pg_get_indexdef(x.indexrelid)\n          from pg_index as x\n          join pg_class as i on i.oid = x.indexrelid\n          join pg_class as c on c.oid = x.indrelid\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s)\n         order by 1, 2, 3\n        ', ), Category( "triggers", 3, '\n        select n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)\n          from pg_trigger as t\n          join pg_class as c on c.oid = t.tgrelid\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s) and not t.tgisinternal\n         order by 1, 2, 3\n        ', ), Category( "routines", 3, '\n        select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),\n               p.prokind, md5(p.prosrc)\n          from pg_proc as p\n          join pg_namespace as n on n.oid = p.pronamespace\n         where n.nspname = any(%(schemas)s)\n         order by 1, 2, 3\n        ', ), Category( "views", 2, "\n        select n.nspname, c.relname, pg_get_viewdef(c.oid, true)\n          from pg_class as c\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s) and c.relkind in ('v', 'm')\n         order by 1, 2\n        ", ), Category( "sequences", 2, '\n        select n.nspname, c.relname,\n               s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcycle\n          from pg_sequence as s\n          join pg_class as c on c.oid = s.seqrelid\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%(schemas)s)\n         order by 1, 2\n        ', ), ); Fingerprint = dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]
def _schemas(connection: psycopg.Connection) -> list[str]: return [row[0] for row in connection.execute(_ALL_SCHEMAS).fetchall()]
def fingerprint(connection: psycopg.Connection, schemas: Sequence[str]) -> Fingerprint:
    compared = [name for name in schemas if name not in RUNTIME_OWNED_SCHEMAS]; result: Fingerprint = {}
    for category in CATEGORIES: rows = connection.execute(category.statement, {"schemas": compared}).fetchall(); result[category.name] = {tuple(row[: category.key_width]): tuple(row[category.key_width :]) for row in rows}
    return result
def _shape(shape: tuple[Any, ...]) -> str: return " | ".join("-" if part is None or part == "" else str(part) for part in shape)
def _render(key: tuple[Any, ...], shape: tuple[Any, ...]) -> str: return f"{'.'.join(str(part) for part in key)}  [{_shape(shape)}]"
def differences(live: Fingerprint, declared: Fingerprint) -> list[str]:
    report: list[str] = []
    for category in CATEGORIES:
        mine = live[category.name]; theirs = declared[category.name]; missing = sorted(set(theirs) - set(mine)); unexpected = sorted(set(mine) - set(theirs)); changed = sorted(key for key in set(mine) & set(theirs) if mine[key] != theirs[key])
        for key in missing: report.append(f"{category.name}: MISSING   {_render(key, theirs[key])}")
        for key in unexpected: report.append(f"{category.name}: EXTRA     {_render(key, mine[key])}")
        for key in changed: report.append( f"{category.name}: DIFFERS   {'.'.join(str(part) for part in key)}\n      declared: {_shape(theirs[key])}\n      live:     {_shape(mine[key])}" )
    return report
def _with_database(conninfo: str, database: str) -> str: params: dict[str, Any] = conninfo_to_dict(conninfo) | {"dbname": database}; return make_conninfo(**params)
def target_locality(database_url: str) -> tuple[str, str]:
    completed = subprocess.run( ["sh", str(LOCAL_TARGET)], input=database_url, capture_output=True, text=True, check=False, timeout=30, )
    if completed.returncode != 0: raise RuntimeError(completed.stderr.strip() or f"{LOCAL_TARGET} exited {completed.returncode}")
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout)); return parsed["ta_locality"], parsed["ta_redacted"]
@contextmanager
def reference_database(server_url: str, *, keep: bool = False) -> Iterator[str]:
    locality, redacted = target_locality(server_url)
    if locality != "local" and os.environ.get(REMOTE_REFERENCE_ENV) != "1": raise RuntimeError( f'{redacted} is not a local server, and building a reference there means CREATE DATABASE followed by DROP DATABASE ... WITH (FORCE) on it. Two ways forward: pass --reference-database-url naming a database that already ran the chain (nothing is created anywhere, and the target stays read-only), or set {REMOTE_REFERENCE_ENV}=1 to build the reference on that server anyway.' )
    admin_url = _with_database(server_url, "postgres"); name = f"truealpha_chain_reference_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin: admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try: reference_url = _with_database(server_url, name); apply_migration_chain(reference_url); yield reference_url
    finally:
        if keep: print(f"schema_drift: kept the reference database {name}", file=sys.stderr)
        else:
            with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as admin: admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
def compare(database_url: str, reference_url: str) -> tuple[list[str], dict[str, int]]:
    with psycopg.connect(reference_url, connect_timeout=10) as reference: declared_schemas = _schemas(reference); declared = fingerprint(reference, declared_schemas)
    if len(declared["relations"]) < MINIMUM_REFERENCE_RELATIONS: raise RuntimeError( f"the reference database declares only {len(declared['relations'])} relations (expected at least {MINIMUM_REFERENCE_RELATIONS}) — the migration chain did not reach it, so there is nothing to compare against and a clean report would mean nothing" )
    with psycopg.connect(database_url, connect_timeout=10) as live: live_schemas = _schemas(live); observed = fingerprint(live, declared_schemas)
    report = [f"schemas: MISSING   {name}" for name in sorted(set(declared_schemas) - set(live_schemas))] + [ f"schemas: EXTRA     {name}" for name in sorted(set(live_schemas) - set(declared_schemas)) ]; report += differences(observed, declared); counts = {"schemas": len(declared_schemas)} | {name: len(declared[name]) for name in declared}; return report, counts
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="", formatter_class=argparse.RawDescriptionHelpFormatter); parser.add_argument( "--database-url", default=os.environ.get("DATABASE_URL"), help="the database to check (default: $DATABASE_URL). Only read from.", ); parser.add_argument( "--reference-database-url", default=None, help=( f"an already-migrated database to compare against. Omitted, one is built on the target's OWN server from db/migrations + db/roles.sql and dropped afterwards — which is refused for a non-local server unless {REMOTE_REFERENCE_ENV}=1, because it creates and force-drops a database there." ), ); parser.add_argument( "--keep-reference", action="store_true", help="leave the reference database behind so it can be inspected or reused.", ); arguments = parser.parse_args(argv)
    if not arguments.database_url: parser.error("--database-url (or DATABASE_URL) is required")
    try:
        if arguments.reference_database_url: report, counts = compare(arguments.database_url, arguments.reference_database_url)
        else:
            with reference_database(arguments.database_url, keep=arguments.keep_reference) as reference_url: report, counts = compare(arguments.database_url, reference_url)
    except (psycopg.Error, RuntimeError, AssertionError) as error: print(f"schema_drift: cannot compare: {error}", file=sys.stderr); return 2
    measured = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    if not report: print(f"schema drift: none. The chain declares {measured}; the database matches all of it."); return 0
    print(f"schema drift: {len(report)} difference(s) against the declared chain.", file=sys.stderr); print(f"The chain declares {measured}.", file=sys.stderr)
    for line in report: print(f"  {line}", file=sys.stderr)
    print( '\nReplay (`make db-migrate`) does not repair this: `create ... if not exists` is a no-op on a relation that already exists in a superseded shape. `make db-reset` recreates the database from the chain, which is what a fresh CI job starts from.', file=sys.stderr, ); return 1
if __name__ == "__main__": raise SystemExit(main())
