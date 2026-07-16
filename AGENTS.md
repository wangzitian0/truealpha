# TrueAlpha Agent Contract

> **Protected file**: AI may modify this file only with explicit user authorization.
> **Repository language**: Code, commits, branches, pull requests, and issues are written
> in English. User-facing conversation may follow the user's language.
> **Architecture authority**: `init.md` wins on architecture and public contracts;
> `vision.md` wins on product scope. `CLAUDE.md` is a symlink to this file and
> `GEMINI.md` delegates here.

## How work happens

Development is conventional: an issue describes the goal, one pull request delivers it,
tests prove it, review and green CI gate the merge.

1. **One issue, one PR.** Every PR references its issue (`Closes #N` when it completes
   the issue, a plain `#N` reference otherwise). Keep PRs small and reviewable.
2. **Issues are the shared coordination surface.** Record verified findings, decisions,
   and next actions on the issue as you learn them. Anyone (human or agent) may improve
   an issue. `governance/capabilities/` holds the dependency graph between capability
   issues as information, not enforcement.
3. **Parallel agents** work on disjoint files. Prefix your issue and PR titles with your
   workspace name (for example `[truealpha-factors]`) so lanes are visible. If two lanes
   need the same file, coordinate through the issue rather than racing; shared surfaces
   (migrations, registries, public exports, lockfiles) deserve a heads-up comment before
   you touch them.
4. **Before starting**: `git status --short --branch`, sync `main`, check open issues and
   PRs for the same work. Before declaring a PR merge-ready, the configured code-review
   process must have completed for the exact head SHA; an empty thread list before that
   completion is not evidence of a clean review. An actionable finding without a High,
   Medium, or Low severity label blocks readiness. Evaluate the unresolved budget of
   High = 0, Medium <= 2, and Low <= 4 on the exact head immediately before merge
   readiness is declared. A green `ci-required`, a deployable `main`, and
   backward-compatible migrations are also required.
5. **Data and evidence stay verifiable.** Captured corpora, snapshots, handoff records,
   and evaluation evidence carry content hashes so a replay provably uses the same bytes.
   Records live under `governance/` (see its README); they document what happened and are
   never a merge gate.

At the start of a task and after context compaction: re-read the user's latest
instruction, run the checkpoint commands above, and note (issue number, branch, files you
intend to touch) before editing. When handing off, leave a short note on the issue: what
is done, what is verified, what is next, what failed and why.

## Project context

TrueAlpha is a fundamental and supply-chain research monorepo: immutable raw source
capture, Postgres warehouse and knowledge-graph metadata, factor computation under
Dagster, and typed `mart` consumption through the Web App, MCP, and `/chat`. Read
`vision.md` for the investment questions; read `init.md` before cross-service design,
public contract, schema, or known-risk decisions. Reconnaissance findings live in
`apps/data-engine/samples/README.md`.

Repository shape:

- `apps/data-engine/`: Python source adapters, sweep scripts, dlt, and Dagster assets.
- `apps/llm-service/`: Python FastAPI, MCP first, `/chat` SSE Tier 3.
- `apps/app-web/`: TypeScript/Next.js; reads `mart` through a read-only account.
- `libs/contracts/`: cross-module PIT DTOs and repository/storage/backtest ports.
- `libs/factors/base/`: provenance-neutral PIT factors; modules 1-6.
- `libs/factors/composite/`: factors that reload materialized upstream outputs; module 7.
- `libs/factors/shared/`: KG entity resolution and the shared structured-extraction
  primitive. Do not reimplement extraction per factor.
- `libs/runtime/`: environment/dependency contracts and Postgres/KG/S3 probes.
- `db/migrations/`: the schema source of truth for `raw`, `staging`, `mart`, `dagster`,
  and `app`.
- `db/roles.sql`: database role and permission configuration.
- `governance/`: historical delivery records (capabilities graph, evidence, handoffs).
- `.github/workflows/`: GitHub Actions with path filtering.

## Architecture red lines

- Never commit `.env`, `*.pem`, tokens, credentials, account identifiers, private hosts,
  or secrets in code, fixtures, comments, or docs. Redact live-session output before it
  reaches a tracked file. Secret scanning is a backstop, not permission.
- Point-in-time data distinguishes `valid_time` from `transaction_time` (knowable-at).
  Write `transaction_time` explicitly from a source property, never an insertion-clock
  default. `recorded_at` is ingestion audit time only.
- Never overwrite a point-in-time record. Restatements insert new rows and set
  `is_restatement`; they never update history in place. Parsed facts carry
  `mapping_version` so reparses remain distinguishable from restatements.
- Source fusion never selects the most recently inserted row. The metric registry's
  per-field `source_priority` selects the mart assertion. Backtesting and factors operate
  only on what was knowable at the historical cutoff.
- Immutable source-response bytes live in S3-compatible object storage. Postgres
  `raw.fetches` stores checksums, object pointers, timestamps, and lineage. Apps and LLM
  services never use object storage as a service-to-service data path.
- Never put computation logic outside `libs/factors`. Application and LLM layers perform
  only deterministic formatting and transport over materialized outputs. Screens and the
  three-tier valuation framework are composite factors, not consumer-side rules.
- Factor inputs are provenance-neutral typed records with opaque input identity, subject,
  value/unit/currency and valid period where applicable, confidence, and snapshot cutoff.
  Factor code never sees or branches on vendor, raw reference, accession, rights, source
  priority, or extractor metadata. Composite confidence cannot exceed the minimum consumed
  confidence unless a versioned policy is stricter.
- Never write staging rows without `confidence`. Never use binary floating point where
  monetary precision matters; database monetary columns use `numeric`.
- LLM surfaces use typed `mart` reads only, never raw/staging access, arbitrary SQL, or
  live factor computation. `mart_readonly` enforces the database boundary and
  `ResearchQueryService` enforces allowed queries, pagination, and row limits.
- LLM extraction is a separate versioned, append-only step. Bind model, instructions,
  schema, and decoding settings; store semantic results and evidence spans. Replay never
  silently calls a model. Self-reported confidence is not calibrated evidence without an
  accepted sealed holdout policy.
- Every moomoo request goes through `api_call_ledger`; no module calls the API directly.
  The ledger is throttle and audit infrastructure, not a fictional monthly-call quota.
  The relevant quote/fundamental endpoints use burst rate limits; do not confuse a
  subscription tier ceiling with a call budget. See `init.md` Section 5.
- Moomoo access is Quote API read-only. Every trading context and every order placement,
  modification, cancellation, or trade-unlock operation is forbidden. The public
  repository's security CI must reject trading APIs rather than relying on review alone.
- Consumers of data-engine outputs pin exact snapshot and handoff identities, never
  `latest`.

## Environments and source gotchas

Target topology: Local, GitHub CI, Staging, Production. Staging and Production are
isolated namespaced stacks; infra2 owns external Vault, MinIO, deployment, and promotion.
This repository consumes only released `infra2-sdk` contracts.

| Environment | Postgres | Object storage | Provisioning |
|---|---|---|---|
| Local | `make runtime-up` or localhost | Local MinIO | `make db-migrate`; bucket bootstrap |
| GitHub CI | Ephemeral service container | Ephemeral MinIO container | Per workflow run |
| Staging | `truealpha-postgres-staging`, host loopback `:15432` | Platform MinIO staging, bucket `truealpha-raw` | infra2 release promotion and `apps/data-engine/scripts/setup_vps_ingest.sh` |
| Production | `truealpha-postgres`, host loopback `:15433` | Platform MinIO, bucket `truealpha-raw` | Explicit graduation |

- Current VPS host scripts and direct OpenD loopback access are reconnaissance/bootstrap
  only; they are not scheduled-run evidence.
- SEC XBRL concept tags and units vary across industries. Do not assume one field mapping
  works for every issuer.
- yfinance has no official SLA. Represent that limitation through lower row confidence;
  never make it a critical-path dependency or a provenance branch in factors.
- N-PORT holdings identify positions by CUSIP/ISIN, not ticker/CIK. Resolve identifiers
  through OpenFIGI or equivalent before writing PIT `same_as` KG edges. Use
  `staging.kg_entities`, `staging.kg_identifiers`, and `staging.kg_edges`, not a flat
  symbol-mapping table.
- Build the structured-extraction primitive in `libs/factors/shared` before
  factor-specific extraction. Do not duplicate extraction logic.

## Commands

- Install/check/test: `make install`, `make check`, `make test`.
- Local dependencies: `make runtime-up`, `make runtime-check`.
- Database: `make db-up`, `make db-migrate`.
- Python: `uv sync --all-packages`, `uv run pytest`, `uv run ruff check .`.
- Web: `cd apps/app-web && bun install`, `bun run dev`, `bun run typecheck`,
  `bun run build`.

Reconnaissance/bootstrap ingestion (ordered; the moomoo commands need the OpenD host and
`MOOMOO_LEDGER_BACKEND=postgres`; probe non-US endpoints before a full sweep):

```sh
uv run --package truealpha-data-engine python apps/data-engine/scripts/bootstrap_universe.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_sec_facts.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_moomoo_nonus.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_moomoo_fundamentals.py --dry-run
```

Run the narrowest relevant tests first. Report what was not run and why.
