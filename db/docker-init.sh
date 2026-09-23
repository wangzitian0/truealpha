#!/bin/sh
# docker-compose.yml mounts this into /docker-entrypoint-initdb.d/, where the official
# postgres entrypoint runs it inside the Postgres container itself, over the local
# socket, on a FRESH volume only. It is a wrapper, not an applier: the chain is applied
# by db/apply_migrations.sh here as everywhere else (#984).
#
# An empty host in a libpq URI means "the default", which is the same Unix socket a bare
# `psql --username X --dbname Y` reaches — the form this file used before #984 — so the
# bootstrap connects exactly as it always did, including during initdb when the server
# listens on no TCP address at all.
set -eu

db_dir="${TRUEALPHA_DB_DIR:-/truealpha-db}"
database="${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"
user="${POSTGRES_USER:-postgres}"

# Deliberately not `exec`. The postgres entrypoint runs an init script when it is
# executable and SOURCES it when it is not (docker-library/postgres#450), and an `exec`
# inside a sourced script would replace the entrypoint itself — the temp server would
# never be shut down and the container would never reach its real startup. The file is
# mode 755 in git, so this should never happen; a boot is not the place to depend on a
# file mode surviving every way this repository is checked out and mounted.
TRUEALPHA_DB_DIR="$db_dir" \
MIGRATIONS_DATABASE_URL="postgresql:///${database}?user=${user}" \
    sh "$db_dir/apply_migrations.sh"
