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
set -eu

db_dir="${TRUEALPHA_DB_DIR:-/app/db}"

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

for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
    echo "== $migration"
    # --no-password: a DSN with missing/wrong credentials must fail fast, not hang the
    # container on an interactive password prompt (same guard libs/contracts' db-contract
    # test runners already use for the identical psql-against-DATABASE_URL pattern).
    # app_service_db_password: db/roles.sql applies it to app_service_login when set and
    # non-empty (#432); every other migration file ignores an unused psql variable.
    psql --no-password "$migrations_url" --set ON_ERROR_STOP=1 \
        -v app_service_db_password="${APP_SERVICE_DB_PASSWORD:-}" \
        --file "$migration"
done
