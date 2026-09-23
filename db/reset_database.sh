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
#                                            not the loopback or a local socket.
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
psql_command="${TRUEALPHA_PSQL:-psql}"

target_url="${MIGRATIONS_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -z "$target_url" ]; then
    echo "reset_database.sh: DATABASE_URL (or MIGRATIONS_DATABASE_URL) is required — this command" \
        "drops a database, so it takes no default target" >&2
    exit 1
fi

case "$target_url" in
    postgresql://* | postgres://*) ;;
    *)
        echo "reset_database.sh: the target must be a postgresql:// URI (got '${target_url%%:*}:...')." \
            "A key=value conninfo cannot be rewritten to name the maintenance database." >&2
        exit 1
        ;;
esac

# Split the URI. Only the database name moves (to `postgres`, to drop and create from);
# everything else — user, password, host, port, query parameters — is carried through
# untouched, so the admin connection is the same connection with a different dbname.
scheme="${target_url%%://*}"
rest="${target_url#*://}"
query=""
case "$target_url" in *\?*) query="?${target_url#*\?}" ;; esac
rest="${rest%%\?*}"
authority="${rest%%/*}"
case "$rest" in
    */*) database="${rest#*/}" ;;
    *) database="" ;;
esac
hostport="${authority##*@}"
case "$hostport" in
    \[*) host="${hostport%%\]*}]" ;;
    *) host="${hostport%%:*}" ;;
esac
# Everything before the credentials, with the password removed — safe to print.
redacted="${scheme}://"
case "$authority" in
    *@*) redacted="${redacted}$(printf '%s' "${authority%%:*}" | sed 's/@.*//')@${hostport}" ;;
    *) redacted="${redacted}${hostport}" ;;
esac
redacted="${redacted}/${database}"

if [ -z "$database" ]; then
    echo "reset_database.sh: '$redacted' names no database" >&2
    exit 1
fi
case "$database" in
    postgres | template0 | template1)
        echo "reset_database.sh: refusing to drop the maintenance database '$database'" >&2
        exit 1
        ;;
    *[!A-Za-z0-9_-]*)
        echo "reset_database.sh: '$database' is not a plain identifier; rename it or drop it by hand" >&2
        exit 1
        ;;
esac

# The red line: this command is for a developer's own database and for CI. An empty host
# is a local Unix socket (the compose container's own psql); anything else must be said
# out loud. Root rule: destructive resets belong in a sandbox, never on a shared or
# deployed service.
case "$host" in
    '' | localhost | 127.0.0.1 | '[::1]' | ::1 | /*) ;;
    *)
        if [ "${TRUEALPHA_ALLOW_REMOTE_RESET:-}" != "1" ]; then
            echo "reset_database.sh: '$redacted' is not a local target. This DROPS the database." \
                "If you really mean a remote one, set TRUEALPHA_ALLOW_REMOTE_RESET=1." >&2
            exit 1
        fi
        echo "reset_database.sh: TRUEALPHA_ALLOW_REMOTE_RESET=1 — resetting the REMOTE database $redacted" >&2
        ;;
esac

admin_url="${scheme}://${authority}/postgres${query}"

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
