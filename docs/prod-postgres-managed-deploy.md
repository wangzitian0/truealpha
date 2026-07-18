# Bringing prod Postgres under infra2-managed deploy (#11 / #53)

A **bootstrap** prod Postgres exists (`truealpha-postgres`, VPS `127.0.0.1:15433`, `docker run`,
persistent volume `truealpha-postgres-prod-data`, daily backup + 7d retention, credential
rotated to a host-root-only value). This runbook replaces it with the **infra2-managed** deploy
that staging already uses, without data loss. Only the Vault-admin steps need an operator with
Vault write access.

## How staging is deployed (the pattern to mirror)

Dokploy compose app at
`/etc/dokploy/compose/truealpha-postgres-*/code/truealpha/truealpha/01.postgres/compose.yaml`.
Two services: a `vault-agent` (renders the postgres password from Vault via AppRole into
`/vault/secrets/.env`) and `postgres` (reads it). Parameterized by env:
`ENV`, `ENV_SUFFIX`, `TA_POSTGRES_HOST_PORT`, `VAULT_ADDR`, `VAULT_ROLE_ID`, `VAULT_SECRET_ID`,
`DATA_PATH`, `COMPOSE_PROJECT_NAME`. Staging uses `ENV=staging`, `ENV_SUFFIX=-staging`,
`TA_POSTGRES_HOST_PORT=15432`.

## Prod deploy (mirror staging with prod values)

1. **Vault (admin-only):** create the prod postgres secret + AppRole + policy for the `production`
   env, exactly as `01.postgres/deploy.py` / `vault-policy.hcl` do for staging (the same tooling,
   `ENV=production`). This renders `secrets.ctmpl` for the prod path.
2. **Deploy the compose** for prod: `ENV=production`, `ENV_SUFFIX=` (prod has no suffix, container
   `truealpha-postgres`), `TA_POSTGRES_HOST_PORT=15433`, prod `VAULT_ROLE_ID`/`VAULT_SECRET_ID`.
   Run the same `01.postgres` compose/`deploy.py` targeting the production env.
3. **Adopt the bootstrap data (no loss):** point the managed `postgres` service at the existing
   `truealpha-postgres-prod-data` volume, OR restore the latest dump
   (`/root/backups/truealpha-prod/prod-*.dump`) into the managed instance:
   `pg_restore -U postgres -d truealpha --clean --if-exists <dump>`.
4. **Verify:** `mart.topt_capture_status.complete = t` and
   `select payload->>'gppe' from mart.topt_gppe_results where payload->>'availability'='available'`
   returns `1153614.480937195208819629373924669` (the MVP GOOG GPPE).
5. **Retire the bootstrap:** stop/remove the `docker run` container and delete
   `/root/.truealpha_prod_pgpw`; the managed vault-agent now owns the credential.

## What this closes

- **#11:** prod is Vault-rendered credentials + compose/Dokploy managed + persistent + backed-up,
  replacing the bootstrap without data loss.
- **#53:** the versioned shadow output (the MVP prod run) is now emitted through the managed path.

The only step requiring an operator is Vault admin (step 1) — the DB provisioning, data adoption,
and verification (2–5) are mechanical and scripted by `01.postgres/deploy.py`.
