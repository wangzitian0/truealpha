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
apps/app-web        TS/Bun  Next.js — reads mart via the mart_readonly role, no API hop
common/contracts      Python  point-in-time DTOs + repository/storage/backtest ports
common/factors        Python  the ONLY place computation logic lives (base / composite / shared)
common/runtime        Python  runtime env/dependencies + Postgres/KG/S3 adapters and probes
db                  SQL     raw / staging / mart / dagster / app schemas + mart_readonly role
```

## Quickstart

```bash
make install        # uv sync + bun install
make runtime-up     # Postgres/KG + MinIO with schemas/bucket initialized
cp .env.example .env  # then set SEC_USER_AGENT
make sample         # capture the checked-in SEC reconnaissance corpus
make check          # lint + typecheck + test of what the diff reaches; FULL=1 for all
```

`make stack-up` additionally builds and starts Web + LLM locally. Application
code consumes only `DATABASE_URL` and the S3-compatible `S3_*` contract; local
Compose, GitHub CI, and infra2 may provide different backends behind it.

The DataHub trust layer defines point-in-time multi-source reconciliation and a
row-complete, versioned [data-quality report](docs/datahub-quality-report.md). The
report preserves missing and conflicted requested cells in its denominator and is the
machine-readable input to later mart/dashboard presentation.

Requires: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker.

## DataHub Demand

Factors, strategies, and research modules request a long-lived data service through a
content-addressed, source-neutral demand with a representative sample. See
[`docs/datahub-service-demand.md`](docs/datahub-service-demand.md) for the contract,
TOPT example, quality objectives, and the boundary with confidence calibration and
`infra2-sdk`.

The continuous-confidence formula and its sensitivity table are documented in
[`docs/confidence-calibration.md`](docs/confidence-calibration.md).
Its DTOs live in `common/contracts`.

### Production TOPT orchestration

Dagster (`apps/data-engine/src/data_engine/dagster_defs.py`) is the single orchestration authority for Production TOPT runs.
The scheduled job (`topt_live_pipeline`, driven by `topt_live_schedule`) and the manual trigger sensor (`pipeline_trigger_sensor`) execute capture, core materialization, plausibility checks, and pointer advancement.
Downstream consumers read the governed head through `mart.served_head` and `mart_readonly`.

## Deployment

Deployed to the VPS through [infra2](https://github.com/wangzitian0/infra2)'s IaC.
TrueAlpha does not check out or execute infra2 source. This repo owns the images;
infra2 owns deployed Compose, Vault secrets, Traefik routes, persistent Postgres,
the environment-specific S3-compatible storage binding, and every deployment side
effect. The application pins only the versioned `infra2-sdk` request contract.

Releases follow [`docs/release-protocol.md`](docs/release-protocol.md).
