# TrueAlpha

Personal fundamental & supply-chain investment research tool. Point-in-time
correctness is the core invariant — every output traceable back to what was
actually knowable at a given historical moment.

Read first: [`init.md`](init.md) (section 0 gives the goal; the rest gives the
architecture; it is the authoritative document) → [`AGENTS.md`](AGENTS.md) (working rules).

## Layout

```
apps/data-engine    Python  ingestion into immutable raw objects + Postgres lineage/staging
apps/llm-service    Python  FastAPI: MCP endpoint (priority) + /chat (Tier 3)
apps/app-web        TS/Bun  Next.js — zero-DB frontend calling FastAPI /api/v1/*
common/contracts      Python  point-in-time DTOs + repository/storage/backtest ports
common/factors        Python  the ONLY place computation logic lives (base / composite / shared)
common/runtime        Python  runtime env/dependencies + Postgres/KG/S3 adapters and probes
db                  SQL     raw / staging / mart / ops / dagster / app schemas + role boundaries
```

## Quickstart

```bash
make install        # uv sync + bun install + per-commit hook
cp .env.example .env  # then set SEC_USER_AGENT
make check FILES=<test paths>  # static checks on the changed files + the named tests; CI runs the full suite
```

CI runs the integration tests against Postgres and S3. `make runtime-up` and
`make stack-up` start the local stack only when a task needs one. Application
code consumes only `DATABASE_URL` and the S3-compatible `S3_*` contract; local
Compose, GitHub CI, and infra2 may provide different backends behind it.

The DataHub trust layer defines point-in-time multi-source reconciliation and a
row-complete, versioned [data-quality report](docs/datahub-quality-report.md). The
report preserves missing and conflicted requested cells in its denominator and is the
machine-readable input to later mart/dashboard presentation.

Requires: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh). The local stack uses Podman (or a Docker-compatible runtime).

## DataHub Confidence

The continuous-confidence formula and its sensitivity table are documented in
[`docs/confidence-calibration.md`](docs/confidence-calibration.md).
Its DTOs live in `common/contracts`.

### Scheduled orchestration

Dagster (`apps/data-engine/src/data_engine/dagster_defs.py`) is the single orchestration authority for scheduled runs across all active universes declared in `common/contracts/src/truealpha_contracts/data/environment_contract.v1.json`.
Scheduled jobs and manual trigger sensors execute capture, core materialization, plausibility checks, and pointer advancement.
Downstream consumers read the governed head through `apps/llm-service`, which queries `mart.served_head` with `mart_readonly`.

## Deployment

Deployed to the VPS through [infra2](https://github.com/wangzitian0/infra2)'s IaC.
TrueAlpha does not check out or execute infra2 source. This repo owns the images;
infra2 owns deployed Compose, Vault secrets, Traefik routes, persistent Postgres,
the environment-specific S3-compatible storage binding, and every deployment side
effect. The application pins only the versioned `infra2-sdk` request contract.

Releases follow [`docs/release-protocol.md`](docs/release-protocol.md).
