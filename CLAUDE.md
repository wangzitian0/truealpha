# CLAUDE.md

This file is the context Claude Code loads every session when working in this repo. Keep it lean — don't copy `init.md`'s content in here.

## What This Is

A monorepo for a fundamental and supply-chain research tool: current httpx reconnaissance/bootstrap scripts plus planned dlt ingestion, immutable raw bytes (S3-compatible storage), warehouse/KG metadata (Postgres), factor computation in `libs/factors` under Dagster, and typed mart consumption through the App, MCP, and `/chat`. Gate 0 semantic/data closure is active; Dagster enters with the first Gate 1 executable slice and is the only real recurring scheduler.

Why this exists and what investment questions it answers → read `vision.md`.
Full technical architecture, schema design, release gates, known risks → read `init.md`.
Neither needs to be read every session — read them on demand per "Reference Documents" below.

## Hard Constraints (IMPORTANT — violating these produces bugs that are hard to notice)

- **Point-in-time correctness**: time-series/financial data distinguishes `valid_time` from `transaction_time`. Writes to the staging layer are append-only — never UPDATE or overwrite an old vintage. Factor computation and backtesting always operate on "what was visible as of a given historical point in time."
- **Two time axes on every staging row, and fusion never picks by recency**: `transaction_time` (= knowable-at) is written explicitly from a source property — never the insert clock (no column defaults; a backfill stamped with now() silently corrupts PIT). `recorded_at` is the ingestion clock, audit only. When several sources assert the same field, the mart winner comes from the metric registry's per-field `source_priority` (`libs/contracts` `metrics.py`, init.md Section 6 "Source fusion") — never from which row landed last. Parsed facts also carry `mapping_version` so a reparse is distinguishable from a restatement.
- **Raw storage is split by responsibility**: S3-compatible storage holds immutable source-response bytes; Postgres `raw.fetches` holds checksums, object pointers, timestamps, and lineage. Apps and the LLM never use object storage as a service-to-service data path.
- **Computation logic exists in exactly one place**: PEG / gross-profit-per-employee / supply-chain and other factor logic is implemented once in `libs/factors` and invoked by data-engine/Dagster. `apps/llm-service` and the App read materialized outputs; they never invoke or reimplement factor computation. This includes screening/tagging logic like the three-tier valuation framework — it is a **composite factor** that reloads upstream mart outputs.
- **Factors consume provenance-neutral typed records, never source metadata.** Inputs carry opaque `input_id`, typed subject, value/unit/currency and valid period where relevant, confidence, and snapshot cutoff. Factor code never sees or branches on source, raw reference, accession, or extractor metadata. Composite confidence cannot exceed the minimum consumed confidence; a versioned policy may be stricter.
- **LLM surfaces can only use typed `mart` reads** — never raw/staging, arbitrary SQL, or live factor computation. `mart_readonly` enforces the database boundary; `ResearchQueryService` enforces allowed queries, pagination, and row limits.
- **Every moomoo call must go through the `api_call_ledger` gate**: no module may call moomoo directly, bypassing the gate. The gate is a defensive throttle/audit trail, not an enforcement of a real moomoo-side monthly quota — confirmed 2026-07-10 from moomoo's own docs that fundamental/quote endpoints are rate-limited (bursts per 30s), not capped at a monthly total; "2,000/month" was an earlier mix-up of the *subscription* quota tier ceiling with a call budget. See `init.md` Section 5.
- **moomoo Quote API only — trading is off-limits in this repo, no exceptions.** This is a **public** GitHub repo. Never import or call `OpenSecTradeContext`, `OpenFutureTradeContext`, `OpenCryptoTradeContext`, `place_order`, `modify_order`, `cancel_order`, or `unlock_trade` — this project only ever reads market/fundamental data via `OpenQuoteContext`. Enforced by a CI grep gate (`.github/workflows/security-gate.yml`), not just convention — a PR that adds any of these must fail CI, not just get flagged in review.
- **No real secrets/credentials/account identifiers ever get committed** — not in code, comments, sample fixtures, or docs. This repo is **public**; a moomoo account password/MD5 hash, phone number, VPS IP/hostname, SSH key, or Vault/DB token is fine to type on the VPS itself (private) but must never reach a file that gets `git add`ed. Redact/mask before writing anything derived from a live credential-bearing session into a committed file. CI runs a secret scan (gitleaks) on every push/PR as a backstop — it is not a substitute for not committing secrets in the first place.

## Repo Structure

```
/apps
  /data-engine    Python: source adapters + sweep scripts + Gate 1 dlt/Dagster assets
  /app-web        TypeScript: Next.js, reads the mart schema via a read-only account
  /llm-service    Python: FastAPI, MCP endpoint + /chat SSE endpoint
/libs
  /contracts      Python: cross-module point-in-time DTOs and repository/storage/backtest ports
  /factors        Python: the single implementation of all seven factor modules
    /base         Factors consuming provenance-neutral PIT snapshot views (modules 1-6)
    /composite    Factors reloading other factors' mart outputs (module 7 — three-tier tagging);
                  confidence cannot exceed the minimum consumed input confidence
    /shared       Entity resolution (KG read/write) + LLM structured-extraction primitive — used by
                  both base and composite factors, not reimplemented per module
  /runtime        Python: runtime env/dependency contract, Postgres/KG/S3 probes, raw object storage
/db
  /migrations     Single source of truth for schema (raw / staging / mart / dagster)
  /roles.sql      Database role/permission configuration
```

## Commands

```bash
make install          # uv sync --all-packages + bun install + pre-commit
make runtime-up       # local runtime: Postgres/KG + MinIO + raw bucket (docker compose)
make runtime-check    # probe Postgres, KG tables, and object storage
make db-migrate       # re-apply db/ DDL to a running Postgres (idempotent)
make check            # ruff + mypy + TS typecheck + pytest (DB/S3 integration tests skip without a reachable
                      # runtime locally; CI sets TRUEALPHA_REQUIRE_RUNTIME=1 so silent skips fail there)

# Reconnaissance/bootstrap ingestion — not scheduled Gate evidence; order matters
uv run --package truealpha-data-engine python apps/data-engine/scripts/bootstrap_universe.py     # ETF N-PORTs + OpenFIGI -> KG universe
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_sec_facts.py        # company-facts -> raw (runs anywhere, no quota)
# The two below need the moomoo OpenD host and MOOMOO_LEDGER_BACKEND=postgres:
uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_moomoo_nonus.py     # HK/CN endpoint spot check BEFORE the full sweep
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_moomoo_fundamentals.py [--dry-run]

# TypeScript (apps/app-web)
cd apps/app-web && bun install
cd apps/app-web && bun run dev
cd apps/app-web && bun run typecheck
```

## Environments

The target rollout has Local, GitHub CI, Staging, and Production. This table describes the intended topology, not evidence that every environment or release gate is complete. Staging and Production are parallel isolated stacks on one VPS (`ENV_SUFFIX` namespacing), with infra2 owning the Vault/MinIO/deploy boundary.

| env | postgres | object storage | how it's made |
|---|---|---|---|
| local dev | `make runtime-up` (or any localhost:5432) | MinIO localhost:9000 | `make db-migrate`, bucket auto-created |
| GitHub CI | service container | docker-run MinIO step | per-run, ephemeral (ci-python.yml) |
| staging (VPS) | `truealpha-postgres-staging`, host loopback :15432 | platform MinIO-staging, bucket `truealpha-raw` | infra2 release tag auto-promotes; host runtime via `scripts/setup_vps_ingest.sh` |
| production (VPS) | `truealpha-postgres`, host loopback :15433 | platform MinIO, same bucket name | Gate 4: isolated shadow bootstrap, exact release manifest, then explicit graduation |

The current VPS host scripts reach moomoo OpenD on host loopback and remain reconnaissance/bootstrap tools only. They cannot satisfy a scheduled-run gate. #11 must deploy an immutable data-engine/Dagster artifact and prove a least-privilege OpenD resource boundary with no public or unrelated-workload access before Staging/Production scheduling counts.

## Conventions

- **Deliverables are a complete issue or a MERGEABLE PR — nothing in between.** Mergeable means: CI green; every review comment (Copilot included) addressed AND its thread resolved; re-request review after structural changes; the PR body describes the final state; cross-repo companions consistent at merge time. infra2 is an external deployment authority, never a TrueAlpha source dependency; this repository pins only released `infra2-sdk` contract artifacts. A complete issue carries context, evidence/repro, and acceptance criteria so anyone can pick it up cold.
- Python dependency management via uv, TypeScript via Bun. No moon for repo task orchestration — GitHub Actions with path filtering is enough for CI.
- Code style follows whatever formatter/linter config already exists per language — not repeated here. If no config exists yet, ask rather than inventing one.
- Before editing `db/migrations`, check whether the change violates a hard constraint above, especially point-in-time.

## Gotchas

- SEC XBRL tags for the same concept can be inconsistent across industries — don't assume field names/units are uniform across companies.
- yfinance has no official SLA — this is expressed as a lower `confidence` on its rows, not a separate ad hoc "fallback only" rule; still don't make it a critical-path dependency.
- LLM extraction is a separate versioned, append-only step. Its invocation binds model, prompt/instructions, schema, and decoding settings; downstream `data_version` uses the stored semantic result and evidence spans. Replay never silently calls the model. Self-reported confidence cannot count as calibrated unless the sealed holdout/SLO gate accepts it; a failing gate requires a new reviewed confidence policy.
- N-PORT (ETF holdings) identifies positions by CUSIP/ISIN, not ticker/CIK — resolve through OpenFIGI or similar before writing the `same_as` edge into `staging.kg_edges`. Entity resolution (all ID crosswalks — CIK/ticker/moomoo_code/CUSIP/ISIN) is now a knowledge graph (`staging.kg_entities` + identifier properties in `staging.kg_identifiers` + point-in-time `staging.kg_edges`), not a flat `symbol_mapping` table — don't recreate the old flat table.
- The extraction primitive in `libs/factors/shared` must exist before the gross-profit/headcount and pure-blood-screening modules are built — don't shortcut it by having each module implement its own extraction logic to save time.

## Reference Documents (read on demand)

- `vision.md` — read when unsure why a design choice was made
- `init.md` — read when making cross-service architecture decisions or touching schema
- [`apps/data-engine/samples/README.md`](apps/data-engine/samples/README.md) — initial reconnaissance findings and captured evidence
