# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; import re; import subprocess; import time; import uuid; from collections.abc import Iterator; from dataclasses import dataclass; from pathlib import Path; from urllib.parse import urlsplit, urlunsplit; import psycopg; import pytest; from psycopg import sql; from truealpha_runtime.testing import apply_migration_chain, guard_db_write, load_tool; drift = load_tool("schema_drift"); REPO_ROOT = Path(__file__).resolve().parents[3]; MIGRATION = REPO_ROOT / "db" / "migrations" / "20261006T1325_datahub_retire_empty_planes.sql"; OLD_SHAPE = Path(__file__).resolve().parent / "fixtures" / "retired_planes_old_shape.sql"; RETIRED_TABLES = ( "app.private_research_objects", "app.publication_policies", "app.tenant_memberships", "raw.capture_checkpoints", "raw.recapture_plans", "staging.filing_documents", "staging.headcount_extraction_invocations", "staging.headcount_facts", "staging.mvp_corporate_actions", "staging.mvp_financial_facts", "staging.mvp_issuer_security_links", "staging.mvp_market_prices", "staging.mvp_security_listing_links", "staging.mvp_universe_memberships", "staging.normalized_records", ); RETIRED_FUNCTIONS = ( "staging.validate_normalized_raw_lineage()", "staging.validate_filing_document_projection()", "staging.validate_normalized_restatement()", "staging.validate_headcount_invocation()", "staging.validate_headcount_projection()", "staging.validate_mvp_projection()", "raw.validate_capture_checkpoint_address()", "raw.validate_recapture_plan_address()", "raw.enforce_capture_checkpoint_progress()", "raw.validate_checkpoint_obligation_refs()", "raw.validate_recapture_obligation_refs()", "raw.has_canonical_obligation_ids(text[], boolean)", "raw.has_canonical_text_json_array(jsonb, boolean)", ); LIVE_TABLES = ( "app.tenants", "app.principals", "app.entitlement_grants", "staging.market_prices", "staging.analyst_rating_events", "raw.capture_runs", ); LIVE_PARENTS = ("app.tenants", "app.principals", "raw.capture_runs"); SKIP_BOUND_SECONDS = 30; WAIT_LOCK_TIMEOUT = "45s"
def _named(database: str) -> str: base = urlsplit(guard_db_write()); return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))
def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(_named("postgres"), connect_timeout=5, autocommit=True) as admin: admin.execute(statement)
def _drop(database: str) -> None: _admin(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))
def _scratch_name(kind: str) -> str: return f"truealpha_retire_{kind}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
@dataclass(frozen=True)
class Fresh: name: str; output: str
@pytest.fixture(scope="module")
def fresh_chain() -> Iterator[Fresh]:
    name = _scratch_name("fresh")
    try: _admin(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try: yield Fresh(name, apply_migration_chain(_named(name)))
    finally: _drop(name)
@pytest.fixture(scope="module")
def old_shape_template(fresh_chain: Fresh) -> Iterator[str]:
    name = _scratch_name("template"); _admin(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(fresh_chain.name)))
    try: completed = subprocess.run( ["psql", "--no-password", _named(name), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(OLD_SHAPE)], capture_output=True, text=True, check=False, ); assert completed.returncode == 0, completed.stdout + completed.stderr; yield name
    finally: _drop(name)
def _clone(source: str) -> Iterator[str]:
    name = _scratch_name("clone"); _admin(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(source)))
    try: yield _named(name)
    finally: _drop(name)
@pytest.fixture
def old_shape(old_shape_template: str) -> Iterator[str]: yield from _clone(old_shape_template)
@pytest.fixture
def migrated(fresh_chain: Fresh) -> Iterator[str]: yield from _clone(fresh_chain.name)
def _existing(database_url: str, relations: tuple[str, ...]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection: return {name for name in relations if connection.execute("select to_regclass(%s)", (name,)).fetchone()[0]}
def _existing_functions(database_url: str, functions: tuple[str, ...]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection: return {name for name in functions if connection.execute("select to_regprocedure(%s)", (name,)).fetchone()[0]}
def _level_prefix(level: str) -> str: return rf"{level}:\s+(?:[0-9A-Z]{{5}}:\s+)?"
def _logged(output: str, level: str, message: str) -> bool: return re.search(_level_prefix(level) + re.escape(message), output) is not None
@dataclass(frozen=True)
class Run: returncode: int; output: str; elapsed: float
def _run_file(database_url: str, *extra: str, lock_timeout: str = "10s") -> Run: environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}; environment["PGOPTIONS"] = f"-c lock_timeout={lock_timeout} -c statement_timeout=60s"; started = time.monotonic(); completed = subprocess.run( ["psql", "--no-password", database_url, "-X", "-v", "ON_ERROR_STOP=1", *extra, "-f", str(MIGRATION)], env=environment, capture_output=True, text=True, check=False, timeout=120, ); return Run(completed.returncode, completed.stdout + completed.stderr, time.monotonic() - started)
def _run_file_and_count_lock_waits(database_url: str, *, lock_timeout: str = "10s") -> tuple[Run, int]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}; environment["PGOPTIONS"] = f"-c lock_timeout={lock_timeout} -c statement_timeout=60s"; started = time.monotonic(); process = subprocess.Popen( ["psql", "--no-password", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, ); waits = 0
    with psycopg.connect(database_url, autocommit=True) as watcher:
        while process.poll() is None:
            waits += watcher.execute( "select count(*) from pg_locks where not granted and locktype = 'relation'" ).fetchone()[0]
            if time.monotonic() - started > 60: process.kill(); break
            time.sleep(0.02)
    output, _ = process.communicate(); return Run(process.returncode, output, time.monotonic() - started), waits
def _application_locks(database_url: str) -> str: environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}; completed = subprocess.run( [ "psql", "--no-password", database_url, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", str(MIGRATION), "-c", "select coalesce(string_agg(format('%s %s', c.oid::regclass, l.mode), ', ' order by c.oid::regclass::text), '') from pg_locks as l join pg_class as c on c.oid = l.relation join pg_namespace as n on n.oid = c.relnamespace where l.pid = pg_backend_pid() and l.locktype = 'relation' and n.nspname in ('app', 'raw', 'staging', 'mart', 'ops')", ], env=environment, capture_output=True, text=True, check=False, timeout=120, ); assert completed.returncode == 0, completed.stdout + completed.stderr; return completed.stdout.strip()
def _seed_tenant_membership(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection: connection.execute("insert into app.tenants (tenant_id) values ('tenant:retire-probe')"); connection.execute( "insert into app.principals (principal_id, tenant_id, principal_kind) values ('principal:retire-probe', 'tenant:retire-probe', 'member')" ); connection.execute( "insert into app.tenant_memberships (membership_event_id, tenant_id, principal_id, membership_state, effective_at, recorded_at) values ('membership:retire-probe', 'tenant:retire-probe', 'principal:retire-probe', 'granted', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')" )
def _seed_tenant_memberships(database_url: str, rows: int) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection: connection.execute("insert into app.tenants (tenant_id) values ('tenant:retire-probe')"); connection.execute( "insert into app.principals (principal_id, tenant_id, principal_kind) values ('principal:retire-probe', 'tenant:retire-probe', 'member')" ); connection.execute( "insert into app.tenant_memberships (membership_event_id, tenant_id, principal_id, membership_state, effective_at, recorded_at) select 'membership:retire-probe-' || n, 'tenant:retire-probe', 'principal:retire-probe', 'granted', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z' from generate_series(1, %s) as n", (rows,), )
def test_a_fresh_chain_creates_none_of_the_retired_relations(fresh_chain: Fresh) -> None: url = _named(fresh_chain.name); assert _existing(url, RETIRED_TABLES) == set(); assert _existing_functions(url, RETIRED_FUNCTIONS) == set(); assert "retired table" not in fresh_chain.output, fresh_chain.output[-3000:]; assert "retired function" not in fresh_chain.output, fresh_chain.output[-3000:]; assert _existing(url, LIVE_TABLES) == set(LIVE_TABLES)
def test_a_replay_over_a_clean_database_is_silent_and_takes_no_lock(migrated: str) -> None: assert _application_locks(migrated) == ""; run = _run_file(migrated); assert run.returncode == 0, run.output; assert "retired" not in run.output, run.output
def test_the_first_run_locks_the_live_tables_that_a_retired_table_references(old_shape: str) -> None:
    held = _application_locks(old_shape)
    for parent in LIVE_PARENTS: assert f"{parent} AccessExclusiveLock" in held, held
def test_the_chain_drops_every_retired_relation_from_an_old_shape_database(old_shape: str, fresh_chain: Fresh) -> None:
    assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES), "the fixture must restore the old shape"; assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set(RETIRED_FUNCTIONS); output = apply_migration_chain(old_shape); assert _existing(old_shape, RETIRED_TABLES) == set(); assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()
    for table in RETIRED_TABLES: assert f"retired table {table} dropped" in output, output[-3000:]
    assert "WARNING" not in output, output[-3000:]; report, counts = drift.compare(old_shape, _named(fresh_chain.name)); assert report == []; assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts
def test_a_second_replay_after_the_drop_changes_nothing(old_shape: str) -> None: apply_migration_chain(old_shape); run = _run_file(old_shape); assert run.returncode == 0, run.output; assert "retired" not in run.output, run.output; assert _existing(old_shape, RETIRED_TABLES) == set()
def test_a_table_with_a_row_stays_and_a_warning_names_it(old_shape: str) -> None:
    _seed_tenant_membership(old_shape); output = apply_migration_chain(old_shape); assert _logged(output, "WARNING", "retired table app.tenant_memberships holds at least 1 row(s); it stays"), output[ -3000: ]; assert _existing(old_shape, RETIRED_TABLES) == {"app.tenant_memberships"}
    with psycopg.connect(old_shape) as connection: assert connection.execute("select count(*) from app.tenant_memberships").fetchone() == (1,)
    again = _run_file(old_shape); assert again.returncode == 0, again.output; assert _logged(again.output, "WARNING", "retired table app.tenant_memberships holds at least 1 row(s)"); assert _existing(old_shape, RETIRED_TABLES) == {"app.tenant_memberships"}
def test_the_row_count_stops_at_the_sample_of_1000_rows(old_shape: str) -> None:
    _seed_tenant_memberships(old_shape, rows=1500); run = _run_file(old_shape); assert run.returncode == 0, run.output; assert _logged(run.output, "WARNING", "retired table app.tenant_memberships holds at least 1000 row(s); it stays"); assert _existing(old_shape, RETIRED_TABLES) == {"app.tenant_memberships"}
    with psycopg.connect(old_shape) as connection: assert connection.execute("select count(*) from app.tenant_memberships").fetchone() == (1500,)
def test_each_table_drops_in_its_own_transaction(old_shape: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as admin: admin.execute("create table public.retire_drop_log (table_name text, xact_id text)"); admin.execute( "create function public.retire_drop_logger() returns event_trigger language plpgsql as $$ begin insert into public.retire_drop_log select format('%s.%s', schema_name, object_name), pg_current_xact_id()::text from pg_event_trigger_dropped_objects() where object_type = 'table'; end $$" ); admin.execute("create event trigger retire_drop_probe on sql_drop execute function public.retire_drop_logger()")
    run = _run_file(old_shape); assert run.returncode == 0, run.output
    with psycopg.connect(old_shape) as connection: logged = connection.execute("select table_name, xact_id from public.retire_drop_log").fetchall()
    assert {name for name, _ in logged} == set(RETIRED_TABLES); assert len({xact_id for _, xact_id in logged}) == len(RETIRED_TABLES), logged
def test_a_table_in_use_is_skipped_without_waiting_and_dropped_on_the_next_replay(old_shape: str) -> None:
    with psycopg.connect(old_shape) as reader: reader.execute("select 1 from staging.mvp_market_prices limit 1"); run, waits = _run_file_and_count_lock_waits(old_shape, lock_timeout=WAIT_LOCK_TIMEOUT); reader.rollback()
    assert run.returncode == 0, run.output; assert waits == 0, f"the migration queued a lock request for {waits} polls"; assert run.elapsed < SKIP_BOUND_SECONDS, f"{run.elapsed:.1f}s"; assert re.search( r"WARNING:.*retired table staging\.mvp_market_prices is in use; the next boot tries again", run.output ), run.output; assert "retired table staging.normalized_records stays" in run.output; assert "SQLSTATE 2BP01" in run.output; assert _existing(old_shape, RETIRED_TABLES) == {"staging.mvp_market_prices", "staging.normalized_records"}; assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == { "staging.validate_mvp_projection()", "staging.validate_normalized_raw_lineage()", "staging.validate_normalized_restatement()", }; after = _run_file(old_shape); assert after.returncode == 0, after.output; assert _existing(old_shape, RETIRED_TABLES) == set(); assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()
@pytest.mark.parametrize("parent", LIVE_PARENTS)
def test_a_live_parent_in_use_stops_the_drop_without_waiting(old_shape: str, parent: str) -> None:
    with psycopg.connect(old_shape) as reader: reader.execute(sql.SQL("select 1 from {} limit 1").format(sql.SQL(parent))); run, waits = _run_file_and_count_lock_waits(old_shape, lock_timeout=WAIT_LOCK_TIMEOUT); reader.rollback()
    assert run.returncode == 0, run.output; assert waits == 0, f"the migration queued a lock request on {parent} for {waits} polls"; assert run.elapsed < SKIP_BOUND_SECONDS, f"{run.elapsed:.1f}s"; assert re.search(r"WARNING:.*retired table .* is in use; the next boot tries again", run.output), run.output; children = { "app.tenants": {"app.tenant_memberships", "app.private_research_objects"}, "app.principals": {"app.tenant_memberships", "app.private_research_objects"}, "raw.capture_runs": {"raw.capture_checkpoints"}, }[parent]; assert _existing(old_shape, RETIRED_TABLES) == children; after = _run_file(old_shape); assert after.returncode == 0, after.output; assert _existing(old_shape, RETIRED_TABLES) == set()
_PLAN_COLUMNS = ( "plan_id, selection_cutoff, predicate_sha256, predicate, selected_obligation_ids, planner_version, content_sha256" ); _PLAN_PREDICATE = ( '{"assessment_policy_ids":[],"content_sha256":"d2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1","freshness_states":[],"mapping_versions":[],"parser_versions":[],"partitions":[],"predicate_id":"recapture-predicate:9d70275ce843f58202347c2d9d2649da1487756888b3392667fca49078e9ab6e","semantic_types":[],"source_policy_ids":[],"subject_ids":["listing:xnas:goog"],"terminal_states":[],"universe_refs":[]}' ); _PLAN_OBLIGATION = "capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac"
def _insert_plan(connection: psycopg.Connection, plan_hash: str, content_sha256: str) -> None: connection.execute( f"insert into raw.recapture_plans ({_PLAN_COLUMNS}) values (%s, '2026-04-01T00:00:00Z', 'd2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1', %s::jsonb, array[%s], 'capture-planner:v1', %s)", (f"capture-list-recapture-plan:{plan_hash}", _PLAN_PREDICATE, _PLAN_OBLIGATION, content_sha256), )
_RECAPTURE_FUNCTIONS = { "raw.validate_recapture_plan_address()", "raw.validate_recapture_obligation_refs()", "raw.has_canonical_obligation_ids(text[], boolean)", "raw.has_canonical_text_json_array(jsonb, boolean)", }
@pytest.mark.parametrize("why", ["it holds a row", "a reader uses it"])
def test_a_retained_table_keeps_the_functions_that_its_trigger_calls(old_shape: str, why: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as admin:
        admin.execute("alter table raw.recapture_plans disable trigger validate_recapture_obligation_refs")
        if why == "it holds a row": _insert_plan( admin, "61ae610f9f6ad21f0fbdf51dd4550cc86b58575f363d33ba967868911020ce46", "1b20a95545bb0915fb932af45defd50c5dffa848af607c14abf62d61d564b65b", )
    with psycopg.connect(old_shape) as reader:
        if why == "a reader uses it": reader.execute("select 1 from raw.recapture_plans limit 1")
        run = _run_file(old_shape); reader.rollback()
    assert run.returncode == 0, run.output; assert _existing(old_shape, RETIRED_TABLES) == {"raw.recapture_plans"}, run.output; assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == _RECAPTURE_FUNCTIONS, run.output
    with psycopg.connect(old_shape) as connection:
        with pytest.raises(psycopg.errors.CheckViolation): _insert_plan(connection, "a" * 64, "a" * 64)
        connection.rollback(); connection.execute("set local session_replication_role = replica"); connection.execute("delete from raw.recapture_plans"); connection.execute("set local session_replication_role = origin"); _insert_plan( connection, "61ae610f9f6ad21f0fbdf51dd4550cc86b58575f363d33ba967868911020ce46", "1b20a95545bb0915fb932af45defd50c5dffa848af607c14abf62d61d564b65b", ); assert connection.execute("select count(*) from raw.recapture_plans").fetchone() == (1,); connection.rollback()
_HELPER = "raw.has_canonical_obligation_ids(text[], boolean)"
@pytest.mark.parametrize( "body", [ pytest.param( "begin return RAW.HAS_CANONICAL_OBLIGATION_IDS(array[]::text[], true); end;", id="upper-case call" ), pytest.param("begin return true; end; -- once raw.has_canonical_obligation_ids", id="mention in a comment"), ], )
def test_a_live_function_that_names_a_retired_helper_keeps_it(old_shape: str, body: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as admin: admin.execute(f"create function staging.live_probe_caller() returns boolean language plpgsql as $${body}$$")
    run = _run_file(old_shape); assert run.returncode == 0, run.output; assert _existing(old_shape, RETIRED_TABLES) == set(), run.output; assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == {_HELPER}, run.output; assert _logged(run.output, "NOTICE", f"retired function {_HELPER} stays: the body of another function names it")
    with psycopg.connect(old_shape) as connection: assert connection.execute("select staging.live_probe_caller()").fetchone() == (True,)
def test_a_role_that_row_security_filters_cannot_make_a_table_with_rows_look_empty(old_shape: str) -> None:
    role = f"retire_probe_{uuid.uuid4().hex[:8]}"; role_id = sql.Identifier(role)
    with psycopg.connect(old_shape, autocommit=True) as admin:
        admin.execute(sql.SQL("create role {} nosuperuser nobypassrls nologin").format(role_id)); admin.execute("insert into app.tenants (tenant_id) values ('tenant:rls-probe')"); admin.execute( "insert into app.principals (principal_id, tenant_id, principal_kind) values ('principal:rls-probe', 'tenant:rls-probe', 'member')" ); admin.execute( "insert into app.private_research_objects (resource_id, tenant_id, owner_principal_id, resource_type, object_ref) values ('document:rls-probe', 'tenant:rls-probe', 'principal:rls-probe', 'private_document', 'object:rls-probe')" )
        for table in RETIRED_TABLES: admin.execute(sql.SQL("alter table {} owner to {}").format(sql.SQL(table), role_id))
        admin.execute(sql.SQL("grant usage on schema app, raw, staging to {}").format(role_id))
        for parent in LIVE_PARENTS: admin.execute(sql.SQL("grant truncate on {} to {}").format(sql.SQL(parent), role_id))
    try:
        with psycopg.connect(old_shape) as probe: probe.execute(sql.SQL("set role {}").format(role_id)); assert probe.execute("select count(*) from app.private_research_objects").fetchone() == (0,); probe.rollback()
        run = _run_file(old_shape, "-c", f"set role {role}"); assert run.returncode == 0, run.output; assert re.search(r"WARNING:.*retired table app\.private_research_objects stays", run.output), run.output; assert _existing(old_shape, RETIRED_TABLES) == {"app.private_research_objects"}, run.output
        with psycopg.connect(old_shape) as connection: assert connection.execute("select count(*) from app.private_research_objects").fetchone() == (1,)
    finally:
        with psycopg.connect(old_shape, autocommit=True) as admin: admin.execute(sql.SQL("reassign owned by {} to postgres").format(role_id)); admin.execute(sql.SQL("drop owned by {}").format(role_id)); admin.execute(sql.SQL("drop role {}").format(role_id))
def test_a_dependent_object_stops_the_drop_and_the_boot_survives(old_shape: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as connection: connection.execute( 'create table staging.live_probe (id text primary key, record_id text references staging.normalized_records (normalized_record_id))' ); connection.execute( "create view staging.live_probe_view as select normalized_record_id from staging.filing_documents" )
    run = _run_file(old_shape); assert run.returncode == 0, run.output; assert _logged(run.output, "WARNING", "retired table staging.normalized_records stays"), run.output; assert _logged(run.output, "WARNING", "retired table staging.filing_documents stays"), run.output; assert _existing(old_shape, RETIRED_TABLES) == {"staging.normalized_records", "staging.filing_documents"}
    with psycopg.connect(old_shape) as connection: key = connection.execute( "select count(*) from pg_constraint where conrelid = 'staging.live_probe'::regclass and contype = 'f'" ).fetchone(); assert key == (1,); assert connection.execute("select to_regclass('staging.live_probe_view')").fetchone()[0] is not None
def test_the_live_tables_that_look_dead_survive(old_shape: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as connection: connection.execute("insert into app.tenants (tenant_id) values ('tenant:keep')")
    before = _existing(old_shape, LIVE_TABLES); assert before == set(LIVE_TABLES); run = _run_file(old_shape); assert run.returncode == 0, run.output; assert _existing(old_shape, LIVE_TABLES) == set(LIVE_TABLES)
    with psycopg.connect(old_shape) as connection: assert connection.execute("select count(*) from app.tenants").fetchone() == (1,)
    assert _existing(old_shape, RETIRED_TABLES) == set()
def test_the_first_boot_under_ordinary_locks_skips_the_tables_and_does_not_fail( old_shape: str, monkeypatch: pytest.MonkeyPatch ) -> None:
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_TIMEOUT", "1s"); monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_ATTEMPTS", "1")
    with psycopg.connect(old_shape) as holder:
        relations = holder.execute( "select format('%%I.%%I', n.nspname, c.relname), c.relkind from pg_class as c join pg_namespace as n on n.oid = c.relnamespace where n.nspname = any(%s) and c.relkind in ('r', 'p', 'v', 'm') order by 1", (["raw", "staging", "mart", "app", "ops", "dagster"],), ).fetchall(); tables = [name for name, kind in relations if kind in ("r", "p")]; views = [name for name, kind in relations if kind in ("v", "m")]; holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables))))
        for view in views: holder.execute(sql.SQL("select 1 from {} limit 0").format(sql.SQL(view)))
        output = apply_migration_chain(old_shape); holder.rollback()
    assert "LOCK TIMEOUT" not in output; skipped = re.findall(_level_prefix("WARNING") + r"retired table (\S+) is in use; the next boot tries again", output); assert sorted(skipped) == sorted(RETIRED_TABLES), output[-3000:]; assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES); after = _run_file(old_shape); assert after.returncode == 0, after.output; assert _existing(old_shape, RETIRED_TABLES) == set()
