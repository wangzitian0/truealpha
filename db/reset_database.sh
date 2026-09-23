#!/bin/sh
# Bring a LOCAL database to exactly the state a fresh CI job starts from: empty, then
# db/apply_migrations.sh over it. `make db-reset` (#984).
#
# This exists because replay is not a repair path, and the Makefile used to say it was.
# CI's Postgres service declares no volume, so every job begins on an empty cluster and
# the chain is applied once, in order, against nothing. A developer's compose Postgres
# declares `postgres_data:/var/lib/postgresql/data` and keeps whatever it has ever had.
# When a migration is reverted and re-landed in a different shape, `create table if not
# exists` does nothing on that developer's machine and the `alter table` that would have
# reshaped it never applied, so the table keeps its superseded shape — measured on
# staging.market_prices_daily, where it cost 27 UndefinedColumn failures in the
# data-engine suite while CI stayed green (#984, the #932 -> #944 -> #939 chain on #576).
#
# So: drop, create, apply. Zero seed, exactly as CI applies zero seed — tests construct
# what they need, and that property is deliberate (#984 keeps it out of scope).
#
#   DATABASE_URL / MIGRATIONS_DATABASE_URL   the database to recreate. Required: a
#                                            destructive command does not get a default.
#   TRUEALPHA_PSQL                           the psql command, as in apply_migrations.sh.
#   TRUEALPHA_ALLOW_REMOTE_RESET=1           the only way to point this at a host that is
#                                            not the loopback or a local socket. What
#                                            counts as local is decided by
#                                            db/local_target.sh, which tools/schema_drift.py
#                                            also runs — one list, two callers, separate
#                                            overrides, because authorising a scratch
#                                            database beside staging is not the same
#                                            decision as authorising `drop database` on it.
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
psql_command="${TRUEALPHA_PSQL:-psql}"

target_url="${MIGRATIONS_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -z "$target_url" ]; then
    echo "reset_database.sh: DATABASE_URL (or MIGRATIONS_DATABASE_URL) is required — this command" \
        "drops a database, so it takes no default target" >&2
    exit 1
fi

# Split the URI and classify the host through db/local_target.sh — the one place that
# answers "is this server local", shared with tools/schema_drift.py, which creates a
# database on the same server and therefore needs the same answer (#990 review). The DSN
# goes over stdin, not argv, because it carries a password.
parsed="$(printf '%s' "$target_url" | sh "$script_dir/local_target.sh")" || {
    echo "reset_database.sh: the target must be a postgresql:// URI — a key=value conninfo cannot" \
        "be rewritten to name the maintenance database, which is how this command reaches one" >&2
    exit 1
}
eval "$parsed"

if [ -z "$ta_database" ]; then
    echo "reset_database.sh: '$ta_redacted' names no database" >&2
    exit 1
fi
case "$ta_database" in
    postgres | template0 | template1)
        echo "reset_database.sh: refusing to drop the maintenance database '$ta_database'" >&2
        exit 1
        ;;
    *[!A-Za-z0-9_-]*)
        echo "reset_database.sh: '$ta_database' is not a plain identifier; rename it or drop it by hand" >&2
        exit 1
        ;;
esac

# The red line: this command is for a developer's own database and for CI. Root rule —
# destructive resets belong in a sandbox, never on a shared or deployed service.
if [ "$ta_locality" != local ]; then
    if [ "${TRUEALPHA_ALLOW_REMOTE_RESET:-}" != "1" ]; then
        echo "reset_database.sh: '$ta_redacted' is not a local target. This DROPS the database." \
            "If you really mean a remote one, set TRUEALPHA_ALLOW_REMOTE_RESET=1." >&2
        exit 1
    fi
    echo "reset_database.sh: TRUEALPHA_ALLOW_REMOTE_RESET=1 — resetting the REMOTE database $ta_redacted" >&2
fi

admin_url="${ta_scheme}://${ta_authority}/postgres${ta_query}"
database="$ta_database"
redacted="$ta_redacted"

echo "reset_database.sh: recreating $redacted (every row and every object in it is discarded)"
# `with (force)` terminates the sessions still attached — a dev server, a stray psql, a
# Dagster daemon — instead of failing with "database is being accessed by other users"
# and leaving the developer to hunt them down. Postgres 13+.
$psql_command --no-password "$admin_url" -X -q -v ON_ERROR_STOP=1 \
    -c "drop database if exists \"$database\" with (force)" \
    -c "create database \"$database\""

MIGRATIONS_DATABASE_URL="$target_url" DATABASE_URL="$target_url" \
    sh "$script_dir/apply_migrations.sh"

echo "reset_database.sh: $redacted is now exactly what a fresh CI job starts from —" \
    "the declared chain, applied once, over nothing"
