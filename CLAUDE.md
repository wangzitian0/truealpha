# CLAUDE.md

This file is the context Claude Code loads every session when working in this repo. Keep it lean — don't copy `init.md`'s content in here.

## What This Is

A monorepo for a fundamental and supply-chain research tool: ingestion (dlt) + warehouse (Postgres) + factor computation (`libs/factors`, orchestrated by Dagster) + consumption layer (App reads the database directly + LLM chat via MCP / constrained SQL).

Why this exists and what investment questions it answers → read `vision.md`.
Full technical architecture, schema design, implementation phases, known risks → read `init.md`.
Neither needs to be read every session — read them on demand per "Reference Documents" below.

## Hard Constraints (IMPORTANT — violating these produces bugs that are hard to notice)

- **Point-in-time correctness**: time-series/financial data distinguishes `valid_time` from `transaction_time`. Writes to the staging layer are append-only — never UPDATE or overwrite an old vintage. Factor computation and backtesting always operate on "what was visible as of a given historical point in time."
- **Computation logic exists in exactly one place**: PEG / gross-profit-per-employee / supply-chain and other factor logic is implemented once, in `libs/factors`. Both `apps/data-engine` and `apps/llm-service` import from there — never reimplement. This includes screening/tagging logic like the three-tier valuation framework — it's a **composite factor** (reads other factors' mart outputs), not app-layer business logic.
- **Factors consume `(entity_id, value, confidence, as_of)` — never the data's source.** Every staging/KG row has a mandatory `confidence` (0-1); a factor must never branch on "this came from SEC vs. moomoo vs. an LLM extraction." A composite factor's own confidence is `min()` of everything it reads.
- **The LLM can only read the `mart` schema** — never raw/staging. This boundary is enforced by a database role (`mart_readonly`), not an application-layer convention.
- **Every moomoo call must go through the `api_call_ledger` gate**: no module may call moomoo directly, bypassing the gate. The gate is a defensive throttle/audit trail, not an enforcement of a real moomoo-side monthly quota — confirmed 2026-07-10 from moomoo's own docs that fundamental/quote endpoints are rate-limited (bursts per 30s), not capped at a monthly total; "2,000/month" was an earlier mix-up of the *subscription* quota tier ceiling with a call budget. See `init.md` Section 5.
- **moomoo Quote API only — trading is off-limits in this repo, no exceptions.** This is a **public** GitHub repo. Never import or call `OpenSecTradeContext`, `OpenFutureTradeContext`, `OpenCryptoTradeContext`, `place_order`, `modify_order`, `cancel_order`, or `unlock_trade` — this project only ever reads market/fundamental data via `OpenQuoteContext`. Enforced by a CI grep gate (`.github/workflows/security-gate.yml`), not just convention — a PR that adds any of these must fail CI, not just get flagged in review.
- **No real secrets/credentials/account identifiers ever get committed** — not in code, comments, sample fixtures, or docs. This repo is **public**; a moomoo account password/MD5 hash, phone number, VPS IP/hostname, SSH key, or Vault/DB token is fine to type on the VPS itself (private) but must never reach a file that gets `git add`ed. Redact/mask before writing anything derived from a live credential-bearing session into a committed file. CI runs a secret scan (gitleaks) on every push/PR as a backstop — it is not a substitute for not committing secrets in the first place.

## Repo Structure

```
/apps
  /data-engine    Python: dlt pipelines + dagster assets/schedules
  /app-web        TypeScript: Next.js, reads the mart schema via a read-only account
  /llm-service    Python: FastAPI, MCP endpoint + /chat SSE endpoint
/libs
  /contracts      Python: cross-module point-in-time DTOs and repository/storage/backtest ports
  /factors        Python: the single implementation of all seven factor modules
    /base         Factors consuming staging/KG data directly (modules 1-6)
    /composite    Factors consuming other factors' mart outputs (module 7 — three-tier tagging);
                  confidence = min() of all inputs consumed
    /shared       Entity resolution (KG read/write) + LLM structured-extraction primitive — used by
                  both base and composite factors, not reimplemented per module
  /runtime        Python: runtime env/dependency contract, Postgres/KG/S3 probes, raw object storage
/db
  /migrations     Single source of truth for schema (raw / staging / mart / dagster)
  /roles.sql      Database role/permission configuration
```

## Commands

The project is just getting started — once the scaffolding is in place, update this section with real commands. Planned:

```bash
# Python (apps/data-engine, apps/llm-service, libs/*)
uv sync --all-packages
uv run pytest

# Local runtime (Postgres/KG + MinIO) or the full application stack
make runtime-up
make runtime-check
make stack-up

# TypeScript (apps/app-web)
bun install
bun dev
bun test
```

## Conventions

- Python dependency management via uv, TypeScript via Bun. No moon for repo task orchestration — GitHub Actions with path filtering is enough for CI.
- Code style follows whatever formatter/linter config already exists per language — not repeated here. If no config exists yet, ask rather than inventing one.
- Before editing `db/migrations`, check whether the change violates a hard constraint above, especially point-in-time.

## Gotchas

- SEC XBRL tags for the same concept can be inconsistent across industries — don't assume field names/units are uniform across companies.
- yfinance has no official SLA — this is expressed as a lower `confidence` on its rows, not a separate ad hoc "fallback only" rule; still don't make it a critical-path dependency.
- LLM-extraction-based factors (headcount, supply-chain relationships) are inherently non-deterministic — the cache key must be based on "extraction result + schema version," not a hash of the raw LLM output, or Dagster's data_version will mistake sampling noise for a real change. The extraction confidence itself starts as a plain LLM self-reported 0-1 score — don't build a multi-sample self-consistency check until real data shows the self-report is unreliable.
- N-PORT (ETF holdings) identifies positions by CUSIP/ISIN, not ticker/CIK — resolve through OpenFIGI or similar before writing the `same_as` edge into `staging.kg_edges`. Entity resolution (all ID crosswalks — CIK/ticker/moomoo_code/CUSIP/ISIN) is now a knowledge graph (`staging.kg_entities` + identifier properties in `staging.kg_identifiers` + point-in-time `staging.kg_edges`), not a flat `symbol_mapping` table — don't recreate the old flat table.
- The extraction primitive in `libs/factors/shared` must exist before the gross-profit/headcount and pure-blood-screening modules are built — don't shortcut it by having each module implement its own extraction logic to save time.

## Reference Documents (read on demand)

- `vision.md` — read when unsure why a design choice was made
- `init.md` — read when making cross-service architecture decisions or touching schema
- Data availability matrix (a Phase -1 deliverable, not yet produced) — what data each factor can actually get; don't assume until this exists
