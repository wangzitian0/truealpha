# TrueAlpha

Personal fundamental & supply-chain investment research tool. Point-in-time
correctness is the core invariant — every output traceable back to what was
actually knowable at a given historical moment.

Read first: [`vision.md`](vision.md) (why) → [`init.md`](init.md) (architecture,
the authoritative doc) → [`CLAUDE.md`](CLAUDE.md) (working rules).

## Layout

```
apps/data-engine    Python  ingestion into immutable raw objects + Postgres lineage/staging
apps/llm-service    Python  FastAPI: MCP endpoint (priority) + /chat (Tier 3)
apps/app-web        TS/Bun  Next.js — reads the mart schema directly, no API hop
libs/contracts      Python  point-in-time DTOs + repository/storage/backtest ports
libs/factors        Python  the ONLY place computation logic lives (base / composite / shared)
libs/runtime        Python  runtime env/dependencies + Postgres/KG/S3 adapters and probes
db                  SQL     raw / staging / mart / dagster schemas + mart_readonly role
```

## Quickstart

```bash
make install        # uv sync + bun install
make runtime-up     # Postgres/KG + MinIO with schemas/bucket initialized
cp .env.example .env  # then set SEC_USER_AGENT
make sample         # Phase -1: pull SEC samples for DDOG / NICE / SHOP / DUOL
make check          # lint + typecheck + test
```

`make stack-up` additionally builds and starts Web + LLM locally. Application
code consumes only `DATABASE_URL` and the S3-compatible `S3_*` contract; local
Compose, GitHub CI, and infra2 may provide different backends behind it.

Requires: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker.

## Deployment

Deployed to the VPS through [infra2](https://github.com/wangzitian0/infra2)'s IaC
(pinned here as the `repo/` submodule, same as finance_report). This repo owns the
images — `release-images.yml` pushes `ghcr.io/wangzitian0/truealpha-app-web` and
`truealpha-llm-service` on main/tags; infra2's `truealpha/truealpha/` service tree
owns deployed Compose, Vault secrets, Traefik routes, persistent Postgres, and
the environment-specific S3-compatible storage binding. Deploy (from `repo/`):

```bash
python -m tools.deploy_v2 --service truealpha/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service truealpha/app      --type staging --iac-ref vX.Y.Z --domain zitian.party
```

## Status

**Phase 0 — walking skeleton.** Phase -1 reconnaissance and runtime/contracts
foundations are complete. Factor implementations remain registered stubs while
point-in-time ingestion is built. CI is path-filtered per app (`.github/workflows/`).
