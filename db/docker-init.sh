#!/bin/sh
set -eu

db_dir="${TRUEALPHA_DB_DIR:-/truealpha-db}"

for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do
    echo "== $migration"
    psql \
        --username "$POSTGRES_USER" \
        --dbname "$POSTGRES_DB" \
        --set ON_ERROR_STOP=1 \
        --file "$migration"
done
