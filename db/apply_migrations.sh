#!/bin/sh
# THE migration applier: apply db/migrations/*.sql in glob order, then db/roles.sql,
# stopping on the first error. Every call site that brings a database to the declared
# schema runs THIS file (#984):
#
#   1-3  .github/workflows/ci-{python,db,web}.yml    host psql -> the Postgres service
#   4    apps/llm-service/Dockerfile CMD             every staging/production/preview boot
#   5    Makefile db-migrate / db-reset              local: compose exec, or host psql
#   6    db/docker-init.sh                           compose initdb, on a fresh volume only
#   7    apps/data-engine/scripts/setup_vps_ingest.sh   VPS: docker exec into the container
#
# Until #984 those were seven independently written loops, and they had drifted: the
# three CI copies, the Makefile and the compose initdb hook bounded no lock and set no
# statement timeout; the VPS copy alone passed `-q`; and `-v app_service_db_password`
# and MIGRATIONS_DATABASE_URL reached call site 4 and nothing else. Four more copies
# lived in the test suite. `libs/runtime/tests/test_migration_applier.py` fails if a
# second chain loop appears anywhere in the tree.
#
# REPLAY IS NOT A REPAIR PATH. The chain holds 113 `alter table` and 162 `drop <object>`
# statements against 176 `create ... if not exists` (grep -ohiE over db/migrations/*.sql,
# 2026-09-23), so a relation left in a superseded shape stays in that shape forever: the
# `create ... if not exists` does nothing, and the `alter` that would have reshaped it
# already ran against the shape that environment used to have.
# `db/reset_database.sh` (`make db-reset`) is the repair path — it recreates the database
# empty and runs this script, which is exactly what a fresh CI job does — and
# `tools/schema_drift.py` (`make db-check`) is what tells you which of the two you need.
#
# What it connects with:
#
#   MIGRATIONS_DATABASE_URL  the admin-privileged DSN. #432: migrations (DDL, including
#                            db/roles.sql's role/grant management) need an admin
#                            credential, distinct from the scoped app_service_login
#                            runtime credential the app itself connects with. infra2
#                            provisions one for staging/prod.
#   DATABASE_URL             used when MIGRATIONS_DATABASE_URL is unset — local, CI and
#                            preview, where it is still the superuser.
#   TRUEALPHA_PSQL           the psql command, default `psql`. Set it when psql runs
#                            somewhere else: `docker compose exec -T postgres psql`
#                            (Makefile) or `docker exec -i truealpha-postgres psql`
#                            (the VPS). When it is set, each file is streamed on stdin
#                            rather than passed with --file, because the repository path
#                            does not exist wherever that command lands.
#   TRUEALPHA_DB_DIR         the directory holding migrations/ and roles.sql. Defaults to
#                            this script's own directory, which is right in all seven
#                            places: db/ in a checkout, /app/db in the llm-service image,
#                            /truealpha-db in the compose Postgres container.
#   APP_SERVICE_DB_PASSWORD  rotated app_service_login password, applied by roles.sql.
#                            Empty (local, CI, the VPS) is a complete no-op there.
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

# The script's own directory. Right for a checkout (db/), the llm-service image (/app/db)
# and the compose Postgres container (/truealpha-db) alike, so no caller has to say.
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)" || script_dir=""
db_dir="${TRUEALPHA_DB_DIR:-$script_dir}"
# Last resort: the llm-service image bakes the chain here (apps/llm-service/Dockerfile),
# and infra2's compose overrides that image's CMD with an inline entrypoint of its own.
# If that entrypoint ever reaches this script by a route where $0 is not a path, a
# production boot must still find the files rather than fail on an empty glob.
if [ -z "${TRUEALPHA_DB_DIR:-}" ] && [ ! -d "$db_dir/migrations" ] && [ -d /app/db/migrations ]; then
    db_dir=/app/db
fi
if [ ! -d "$db_dir/migrations" ] || [ ! -f "$db_dir/roles.sql" ]; then
    echo "apply_migrations.sh: no migration chain at '$db_dir' (expected migrations/ and roles.sql);" \
        "set TRUEALPHA_DB_DIR" >&2
    exit 1
fi

# psql, or the command that runs psql somewhere else. Deliberately unquoted where it is
# used: it is a command LINE, and word splitting is how the extra arguments reach psql.
psql_command="${TRUEALPHA_PSQL:-psql}"

# Seconds unless a unit is given; passed straight to Postgres.
lock_timeout="${TRUEALPHA_MIGRATION_LOCK_TIMEOUT:-5s}"
statement_timeout="${TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT:-60s}"
# Attempts per file when the failure is a lock timeout (1 = no retry).
lock_attempts="${TRUEALPHA_MIGRATION_LOCK_ATTEMPTS:-3}"
# Backoff before retry n is n * this many seconds (1 s, 2 s, ... by default).
lock_backoff_seconds="${TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS:-1}"
# Total seconds the whole run may spend in failed lock-timeout attempts and backoff.
lock_budget_seconds="${TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS:-25}"

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
if [ "$lock_attempts" -lt 1 ]; then
    echo "apply_migrations.sh: TRUEALPHA_MIGRATION_LOCK_ATTEMPTS must be at least 1 (1 = no retry)" >&2
    exit 1
fi
# The two timeouts reach the server as SQL (below) as well as through PGOPTIONS, so they
# are validated as interval literals rather than interpolated as whatever arrives.
for knob in "$lock_timeout" "$statement_timeout"; do
    case "$knob" in
        '' | *[!0-9a-zA-Z]*)
            echo "apply_migrations.sh: timeouts must be a bare Postgres interval such as 5s or 500ms (got '$knob')" >&2
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
    $psql_command --no-password "$migrations_url" -X -q -A -F ' | ' -P footer=off \
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

# One file, one session.
#
# --no-password: a DSN with missing/wrong credentials must fail fast, not hang the
# container on an interactive password prompt (same guard libs/contracts' db-contract
# test runners already use for the identical psql-against-DATABASE_URL pattern).
# app_service_db_password: db/roles.sql applies it to app_service_login when set and
# non-empty (#432); every other migration file ignores an unused psql variable.
# VERBOSITY=verbose prints the SQLSTATE, which is what classifies the failure below
# regardless of the server's message language.
run_one_file() {
    if [ -n "${TRUEALPHA_PSQL:-}" ]; then
        # psql is running elsewhere — `docker exec`, `docker compose exec` — so two things
        # change and nothing else does. The repository path does not exist there, so the
        # bytes go over stdin (psql then reports errors as `psql:<stdin>:<line>:`, which
        # the location parser below reads exactly as it reads `<file>:<line>:`). And that
        # psql is a different process tree that inherits no PGOPTIONS from here, so the
        # same two bounds are carried as SQL instead: psql executes -c and -f in the
        # order given, in one session, so they bind the file that follows them.
        #
        # `-f -` is load-bearing, not decoration. psql does not read standard input at
        # all once -c or -f is given, so `psql -c "set ..." < file` runs the two SETs,
        # ignores the file and EXITS 0 — a transport that reports success while applying
        # nothing (measured 2026-09-23, before this line existed). `-f -` is what names
        # stdin as the file to run. test_schema_drift.py applies the chain to an empty
        # database through this branch and diffs the result, so the next person to touch
        # it gets a red test rather than an empty database.
        #
        # The deployed boot (call site 4) takes the branch below; its log is unchanged.
        $psql_command --no-password "$migrations_url" --set ON_ERROR_STOP=1 \
            -v VERBOSITY=verbose \
            -v app_service_db_password="${APP_SERVICE_DB_PASSWORD:-}" \
            -c "set lock_timeout = '${lock_timeout}'" \
            -c "set statement_timeout = '${statement_timeout}'" \
            -f - < "$1"
    else
        $psql_command --no-password "$migrations_url" --set ON_ERROR_STOP=1 \
            -v VERBOSITY=verbose \
            -v app_service_db_password="${APP_SERVICE_DB_PASSWORD:-}" \
            --file "$1"
    fi
}

apply_file() {
    migration="$1"
    attempt=1
    while :; do
        attempt_started="$(date +%s)"
        if run_one_file "$migration" >"$attempt_log" 2>&1; then
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

# An unmatched glob leaves the pattern itself in the list, which psql would report as a
# missing file — but only after the run has already claimed to be applying something.
# Count first, so "it applied nothing and said so" cannot read as success.
chain_length=0
for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
    if [ ! -f "$migration" ]; then
        case "$migration" in
            *'*.sql')
                echo "apply_migrations.sh: '$db_dir/migrations' holds no .sql files — refusing to report" \
                    "success over an empty chain" >&2
                ;;
            *)
                echo "apply_migrations.sh: '$migration' is not a file — the chain at '$db_dir' is incomplete" >&2
                ;;
        esac
        exit 1
    fi
    chain_length=$((chain_length + 1))
done
echo "apply_migrations.sh: applying $chain_length files from $db_dir (migrations in glob order, then roles.sql)"

for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
    echo "== $migration"
    apply_file "$migration" || exit 1
done

echo "apply_migrations.sh: applied in $(($(date +%s) - run_started))s (lock waits ${lock_waited}s)"
