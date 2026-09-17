#!/bin/sh
# Idempotent schema migration runner for an APP CONTAINER connecting to a remote/
# external Postgres via DATABASE_URL (staging, prod, or a preview's own ephemeral DB —
# see infra2 truealpha/truealpha/preview/compose.yaml).
#
# Companion to db/docker-init.sh (which mounts into /docker-entrypoint-initdb.d/ and
# runs AS the postgres process itself, peer-auth, for local dev via docker-compose.yml).
# This script instead runs FROM an app image (baked in by apps/llm-service/Dockerfile)
# and connects over the network via $DATABASE_URL, so it works identically whether that
# URL points at a fresh ephemeral preview DB or the real staging/prod truealpha-postgres.
#
# Mirrors the exact `psql ... -v ON_ERROR_STOP=1 -f "$f"` loop already proven in
# Makefile's db-migrate target and .github/workflows/ci-db.yml — same semantics, same
# idempotent SQL files, just parameterized on DATABASE_URL instead of
# POSTGRES_USER/POSTGRES_DB peer auth. The first statement error in any file aborts the
# whole run (fail closed); every migration file and roles.sql is itself written to be
# safe to re-run (create schema if not exists / catch duplicate_object).
#
# A boot never waits on a lock (2026-09-17 incident). The v0.0.81 production boot sat
# 108 s in 0017_contract_objects.sql behind a backfill's open transaction, the llm
# healthcheck gave up, the rollout failed and the API stayed down until the backfill
# committed. Two things stop that from recurring:
#
#   * the migrations only take a lock when the catalog says there is something to change
#     (libs/runtime/tests/test_migration_boot_locks.py replays the chain while another
#     session holds a lock on every table and view), and
#   * this runner bounds every wait. Each session runs with lock_timeout and
#     statement_timeout. A file that times out on a LOCK is retried from its first
#     statement (safe: every file is idempotent) a bounded number of times, with backoff,
#     inside an overall lock-wait budget; the log names the file, the statement's line,
#     and the transactions that held the locks. Anything else — a statement timeout, a
#     SQL error, an exhausted budget — exits non-zero, so the container restarts and the
#     rollout fails loudly instead of the boot hanging or silently skipping a file.
#
# The defaults keep a contended boot inside the llm healthcheck window (infra2
# 10.app/compose.yaml: start_period 30 s, then 3 probes 10 s apart, so roughly 60 s from
# start to `unhealthy`). A clean replay takes ~15 s on production; the lock budget adds at
# most 25 s of waiting plus the attempt that exhausts it (one lock_timeout), so the runner
# gives up by ~45 s. A statement that legitimately needs longer than statement_timeout
# does not belong in a boot migration (#914 moved the entity backfill to a Dagster job
# for exactly that reason); an operator running one by hand can raise the knob.
set -eu

db_dir="${TRUEALPHA_DB_DIR:-/app/db}"

# Seconds unless a unit is given; passed straight to Postgres.
lock_timeout="${TRUEALPHA_MIGRATION_LOCK_TIMEOUT:-5s}"
statement_timeout="${TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT:-60s}"
# Attempts per file when the failure is a lock timeout (1 = no retry).
lock_attempts="${TRUEALPHA_MIGRATION_LOCK_ATTEMPTS:-3}"
# Backoff before retry n is n * this many seconds (1 s, 2 s, ... by default).
lock_backoff_seconds="${TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS:-1}"
# Total seconds the whole run may spend in failed lock-timeout attempts and backoff.
lock_budget_seconds="${TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS:-25}"

# #432: migrations (DDL, including db/roles.sql's role/grant management) need an
# admin-privileged credential, distinct from the scoped app_service_login runtime
# credential the app itself is meant to connect with. MIGRATIONS_DATABASE_URL is that
# admin credential where infra2 has provisioned one (staging/prod); local/CI/preview
# fall back to DATABASE_URL, which is still the superuser there.
migrations_url="${MIGRATIONS_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -z "$migrations_url" ]; then
    echo "apply_migrations.sh: MIGRATIONS_DATABASE_URL or DATABASE_URL is required" >&2
    exit 1
fi

for knob in "$lock_attempts" "$lock_backoff_seconds" "$lock_budget_seconds"; do
    case "$knob" in
        '' | *[!0-9]*)
            echo "apply_migrations.sh: attempts, backoff and budget must be whole numbers (got '$knob')" >&2
            exit 1
            ;;
    esac
done

# libpq reads PGOPTIONS for every connection psql opens, so the timeouts bind the
# migration session itself — including statements inside DO blocks and functions. A
# DSN that sets its own `options` takes precedence, as libpq documents.
PGOPTIONS="${PGOPTIONS:-} -c lock_timeout=${lock_timeout} -c statement_timeout=${statement_timeout}"
# An unreachable host fails the boot in bounded time instead of hanging on TCP.
PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
export PGOPTIONS PGCONNECT_TIMEOUT

attempt_log="$(mktemp)"
trap 'rm -f "$attempt_log"' EXIT INT TERM

run_started="$(date +%s)"
lock_waited=0

# Who held the locks: every other transaction holding a lock on a table or view in an
# application schema, oldest first, one line each with the relations and modes it holds.
# Read-only, and bounded by the same timeouts.
report_lock_holders() {
    psql --no-password "$migrations_url" -X -q -A -F ' | ' -P footer=off \
        -c "select a.pid, coalesce(nullif(a.application_name, ''), '-') as application,
                   a.usename, a.state,
                   date_trunc('second', now() - a.xact_start) as transaction_age,
                   left(regexp_replace(a.query, '\\s+', ' ', 'g'), 120) as last_query,
                   string_agg(format('%I.%I(%s)', n.nspname, c.relname, replace(l.mode, 'Lock', '')),
                              ', ' order by n.nspname, c.relname) as holds
              from pg_locks as l
              join pg_stat_activity as a on a.pid = l.pid
              join pg_class as c on c.oid = l.relation
              join pg_namespace as n on n.oid = c.relnamespace
             where l.granted
               and l.locktype = 'relation'
               and l.pid <> pg_backend_pid()
               and c.relkind in ('r', 'p', 'v', 'm')
               and n.nspname not in ('pg_catalog', 'information_schema')
               and n.nspname not like 'pg_t%'
             group by a.pid, a.application_name, a.usename, a.state, a.xact_start, a.query
             order by a.xact_start nulls last, a.pid
             limit 20" >&2 || echo "apply_migrations.sh: (could not list lock holders)" >&2
}

apply_file() {
    migration="$1"
    attempt=1
    while :; do
        attempt_started="$(date +%s)"
        # --no-password: a DSN with missing/wrong credentials must fail fast, not hang the
        # container on an interactive password prompt (same guard libs/contracts' db-contract
        # test runners already use for the identical psql-against-DATABASE_URL pattern).
        # app_service_db_password: db/roles.sql applies it to app_service_login when set and
        # non-empty (#432); every other migration file ignores an unused psql variable.
        # VERBOSITY=verbose prints the SQLSTATE, which is what classifies the failure below
        # regardless of the server's message language.
        if psql --no-password "$migrations_url" --set ON_ERROR_STOP=1 \
            -v VERBOSITY=verbose \
            -v app_service_db_password="${APP_SERVICE_DB_PASSWORD:-}" \
            --file "$migration" >"$attempt_log" 2>&1; then
            cat "$attempt_log"
            return 0
        fi
        cat "$attempt_log" >&2
        now="$(date +%s)"
        if ! grep -q '55P03' "$attempt_log"; then
            # A statement timeout (57014), a SQL error, a lost connection: retrying the
            # same bytes would fail the same way or hide a real defect.
            echo "apply_migrations.sh: FAILED $migration (not a lock timeout; no retry)" >&2
            return 1
        fi
        lock_waited=$((lock_waited + now - attempt_started))
        # psql prefixes the error with `<file>:<line>:`, the line the failing statement ends on.
        location="$(grep -m 1 -o "[^ ]*:[0-9]*: ERROR" "$attempt_log" | sed 's/: ERROR$//' || true)"
        line="${location##*:}"
        echo "apply_migrations.sh: LOCK TIMEOUT (lock_timeout=${lock_timeout}) in $migration" \
            "at line ${line:-?}, attempt $attempt/$lock_attempts;" \
            "lock waits so far ${lock_waited}s of a ${lock_budget_seconds}s budget" >&2
        if [ -n "$line" ] && [ "$line" -gt 0 ] 2>/dev/null; then
            first=$((line > 4 ? line - 4 : 1))
            echo "apply_migrations.sh: the statement ends at line $line (the psql CONTEXT above names it inside a DO block):" >&2
            sed -n "${first},${line}p" "$migration" | sed 's/^/    | /' >&2
        fi
        echo "apply_migrations.sh: transactions holding table/view locks (pid | application | user | state | age | last query | holds):" >&2
        report_lock_holders
        backoff=$((attempt * lock_backoff_seconds))
        if [ "$attempt" -ge "$lock_attempts" ] || [ $((lock_waited + backoff)) -gt "$lock_budget_seconds" ]; then
            echo "apply_migrations.sh: FAILED $migration: still locked after $attempt attempt(s)," \
                "${lock_waited}s of lock waits; exiting so the container restarts and the rollout" \
                "reports it (a long transaction holds a table this boot must change)" >&2
            return 1
        fi
        echo "apply_migrations.sh: retrying $migration from its first statement in ${backoff}s" >&2
        sleep "$backoff"
        lock_waited=$((lock_waited + backoff))
        attempt=$((attempt + 1))
    done
}

for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
    echo "== $migration"
    apply_file "$migration" || exit 1
done

echo "apply_migrations.sh: applied in $(($(date +%s) - run_started))s (lock waits ${lock_waited}s)"
