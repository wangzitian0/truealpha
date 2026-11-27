#!/bin/sh
set -eu

# Tests set SECRETS_ENV_FILE. A deployment never sets it, so the default is the Vault mount.
secrets_file="${SECRETS_ENV_FILE:-/secrets/.env}"
max_wait=60
waited=0
while [ ! -s "$secrets_file" ] && [ "$waited" -lt "$max_wait" ]; do
  sleep 2
  waited=$((waited + 2))
done
[ -s "$secrets_file" ] || { echo "FATAL: Vault secrets were not rendered after ${max_wait}s"; exit 1; }

set -a
# shellcheck source=/dev/null
. "$secrets_file"
set +a

export DAGSTER_POSTGRES_URL="${DATABASE_URL}?options=-csearch_path%3Ddagster"

# Boot validation (#1210). Refuse to start when the process lacks a variable that the image
# manifest declares as injected. The validator checks names only and never prints values.
# The validator is the SDK's own. truealpha v0.2.132 deleted its `truealpha_runtime.boot` re-export,
# and the SDK import also works in the images before it (that module imported the same function).
# A failure exits non-zero here, so "set -e" stops the script before "exec".
python -m infra2_sdk.config validate apps/data-engine/required-env.generated.json >/dev/null

exec "$@"
