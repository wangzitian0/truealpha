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

### Manual Production TOPT core

After an explicitly triggered Production TOPT run has completed all 84 obligations,
freeze its exact snapshot and materialize GPPE v0 plus three-tier valuation with:

```bash
uv run --package truealpha-data-engine python \
  apps/data-engine/scripts/materialize_production_topt_core.py \
  --run-id capture-run:<sha256> \
  --release-manifest-id release-manifest:<sha256> \
  --risk-free-rate 0.05 \
  --confirmation 'MATERIALIZE PRODUCTION TOPT CORE'
```

The command first persists the 20 issuer-level GPPE module-2 outputs, reloads those
immutable outputs, and then persists the tier composite with an exact
`gppe_invocation_id`/`gppe_result_id` lineage edge. It prints the immutable
`snapshot_id` and both base/composite invocation identities. Downstream reads must
supply the exact composite identities; no `latest` read exists:

```bash
uv run --package truealpha-data-engine python \
  apps/data-engine/scripts/query_production_topt_datahub.py \
  --run-id capture-run:<sha256> \
  --read core_results \
  --release-manifest-id release-manifest:<sha256> \
  --universe-id universe:<id> \
  --universe-version <version> \
  --universe-sha256 <sha256> \
  --snapshot-id topt-core-snapshot:<sha256> \
  --invocation-id topt-core-invocation:<sha256>
```

Use `--read status` or `--read meta_info` with the run ID for capture progress,
and `--read core_meta_info` with the exact core identities for issuer-level lineage
(four cells per listing, eight for an issuer with two share classes), including the
materialized GPPE invocation and result identities. Observation freshness in both
snapshot and meta reads is recomputed at the run cutoff from the bound schedule
policy; an `unchanged` reuse cannot preserve an earlier fresh classification.
These commands are manual-only and do not register or activate a schedule.

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

The local tool below validates and renders a staging `DeployRequest v1` as canonical
JSON. It does not send the request. The infra2 receiver is not enabled for TrueAlpha,
and Production requests remain deny-all until infra2 verifies remote evidence.

```bash
uv run python tools/app_deploy_request.py \
  --request-id truealpha-run-12345678 \
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
