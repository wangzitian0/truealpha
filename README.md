# TrueAlpha

Personal fundamental & supply-chain investment research tool. Point-in-time
correctness is the core invariant — every output traceable back to what was
actually knowable at a given historical moment.

Read first: [`vision.md`](vision.md) (why) → [`init.md`](init.md) (architecture,
the authoritative doc) → [`CLAUDE.md`](CLAUDE.md) (working rules).

## Layout

```
apps/data-engine    Python  ingestion into Postgres raw schema (dlt + Dagster from Phase 0/1)
apps/llm-service    Python  FastAPI: MCP endpoint (priority) + /chat (Tier 3)
apps/app-web        TS/Bun  Next.js — reads the mart schema directly, no API hop
libs/factors        Python  the ONLY place computation logic lives (base / composite / shared)
db                  SQL     raw / staging / mart / dagster schemas + mart_readonly role
```

## Quickstart

```bash
make install        # uv sync + bun install
make db-up          # dev Postgres with schemas applied
cp .env.example .env  # then set SEC_USER_AGENT
make sample         # Phase -1: pull SEC samples for DDOG / NICE / SHOP / DUOL
make check          # lint + typecheck + test
```

Requires: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker.

## Deployment

Deployed to the VPS through [infra2](https://github.com/wangzitian0/infra2)'s IaC
(pinned here as the `repo/` submodule, same as finance_report). This repo owns the
images — `release-images.yml` pushes `ghcr.io/wangzitian0/truealpha-app-web` and
`truealpha-llm-service` on main/tags; infra2's `truealpha/truealpha/` service tree
owns the compose, Vault secrets, Traefik routes, and the persistent Postgres at
`/data/truealpha/postgres`. Deploy (from `repo/`):

```bash
python -m tools.deploy_v2 --service truealpha/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service truealpha/app      --type staging --iac-ref vX.Y.Z --domain zitian.party
```

## Status

**Phase -1 — data reconnaissance.** Building the data availability matrix; factor
implementations are registered stubs that fix the function-signature convention.
CI is path-filtered per app (`.github/workflows/`).
