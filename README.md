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
apps/app-web        TS/Bun  Next.js — reads mart via the mart_readonly role, no API hop (strategy runs live; seven-module dashboard read pending #41)
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
make sample         # capture the checked-in SEC reconnaissance corpus
make check          # lint + typecheck + test
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
Its DTOs live in `libs/contracts`. The evaluator and the report builder were
deleted in #1239, because no deployed path called them.

### Production TOPT orchestration

Dagster (`apps/data-engine/src/data_engine/dagster_defs.py`) is the single orchestration authority for Production TOPT runs.
The scheduled job (`topt_live_pipeline`, driven by `topt_live_schedule`) and the manual trigger sensor (`pipeline_trigger_sensor`) execute capture, core materialization, plausibility checks, and pointer advancement.
Downstream consumers read the governed head (`mart.current_pointer`) through `mart_readonly`.
The manual TOPT CLI scripts were deleted in #1272 because Dagster owns all TOPT execution.

## Deployment

Deployed to the VPS through [infra2](https://github.com/wangzitian0/infra2)'s IaC.
TrueAlpha does not check out or execute infra2 source. This repo owns the images;
infra2 owns deployed Compose, Vault secrets, Traefik routes, persistent Postgres,
the environment-specific S3-compatible storage binding, and every deployment side
effect. The application pins only the versioned `infra2-sdk` request contract.

Those two images are the current scaffold, not a complete Production release. Gate 4
requires #11/#52 to add an immutable data-engine/Dagster artifact and bind every service,
migration, catalog/SLO version, and configuration hash in one signed release manifest;
manual host sweeps cannot satisfy scheduled or promotion evidence.

The local tool below validates and renders a release-only `DeployRequest v1` as canonical
JSON. It has no transport or deployment side effects. The manual `Deploy release`
workflow sends that exact payload to infra2 and succeeds only after the matching infra2
receiver run succeeds; its receiver URL is the staging evidence required for Production.
The workflow remains intentionally unusable until infra2 issue #500 enables the
TrueAlpha receiver/profile and exact receipt identity.

```bash
uv run python tools/app_deploy_request.py \
  --request-id truealpha-run-12345678 \
  --deploy-type staging \
  --version-ref v1.2.3 \
  --source-sha 1234567890abcdef1234567890abcdef12345678 \
  --source-run-url https://github.com/wangzitian0/truealpha/actions/runs/12345678 \
  --source-run-id 12345678
```

## Status

**Walking skeleton; Gate 0 is active.** Initial reconnaissance and runtime/contracts
foundations exist, but the executable interfaces are not frozen until semantic, source,
lineage, research-oracle, and coverage/SLO closure issues #56-#61 pass. Factor
implementations remain registered stubs while the point-in-time path is built. CI is
path-filtered per app (`.github/workflows/`).
