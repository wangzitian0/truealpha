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

if [ -z "${DATABASE_URL:-}" ]; then
    echo "apply_migrations.sh: DATABASE_URL is required" >&2
    exit 1
fi

# TEMPORARY DIAGNOSTIC (#455 follow-up, remove after use): staging's llm-service
# crash-loops with ExitCode 1 and no reachable log surface (Dokploy's REST API has no
# log-content endpoint under the available credentials; SigNoz has zero truealpha
# entries because the crash happens here, before the Python/OTEL app ever starts).
# Capture the migration run's own output and, on failure, serve it over :8000 instead
# of exiting -- Traefik can then discover a stable (if unhealthy) container and route
# /api/* to it, making the exact psql error readable via plain curl. On success this
# is a no-op; the log is discarded and uvicorn starts exactly as before.
migration_log="$(mktemp)"
if ! (
    for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
        echo "== $migration"
        # --no-password: a DSN with missing/wrong credentials must fail fast, not hang
        # the container on an interactive password prompt (same guard libs/contracts'
        # db-contract test runners already use for the identical psql-against-DATABASE_URL
        # pattern).
        psql --no-password "$DATABASE_URL" --set ON_ERROR_STOP=1 --file "$migration"
    done
) >"$migration_log" 2>&1; then
    cat "$migration_log" >&2
    # Redact the DSN password before this ever reaches a public HTTP response --
    # migration failures are almost always SQL errors (connectivity already proved
    # itself by getting this far), but a malformed-DSN edge case could otherwise echo
    # the superuser credential back verbatim.
    db_password=$(printf '%s' "$DATABASE_URL" | sed -n 's#.*://[^:]*:\([^@]*\)@.*#\1#p')
    if [ -n "$db_password" ]; then
        sed "s#$db_password#[redacted]#g" "$migration_log" > "$migration_log.redacted"
        mv "$migration_log.redacted" "$migration_log"
    fi
    exec python -c "
import http.server
import socketserver

body = open('$migration_log', 'rb').read()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(body)

with socketserver.TCPServer(('0.0.0.0', 8000), Handler) as httpd:
    httpd.serve_forever()
"
fi
