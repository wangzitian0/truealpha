# TrueAlpha Agent Operating Contract

> **Protected file**: AI may modify this file only with explicit user authorization.
> **Repository language**: Code, commits, branches, pull requests, issues, manifests,
> and repository reports are written in English. User-facing conversation may follow
> the user's language.
> **Architecture authority**: `init.md` wins on architecture and public contracts.

This file is the short, tool-neutral entry point for Codex, Claude Code, Gemini, and
human contributors. It defines how an agent chooses work, coordinates through issues,
and preserves state across context compaction. Detailed delivery and governance rules
live in the referenced SOPs; do not copy them back into this file.

## Authority And SOP Map

Use the narrowest authoritative source for the decision at hand:

1. The user's latest explicit instruction defines the current task and may grant a
   named exception. An exception is narrow; it does not transfer to another PR or turn.
2. `init.md` defines architecture, module boundaries, and public interfaces.
3. `vision.md` defines product scope and capability outcomes.
4. This `AGENTS.md` defines agent operating, ownership, and handoff rules.
5. `docs/iterative-delivery.md` defines the E0-E5 evidence ladder, lanes,
   dependencies, batches, and handoffs.
6. `governance/README.md` defines canonical governance artifacts and validation.
7. `governance/gate0/README.md` and `governance/handoffs/README.md` are specialist
   runbooks.
8. `CLAUDE.md` is a relative symlink to this file so Claude Code receives this exact
   contract. `GEMINI.md` is a thin adapter that delegates here.

The repository therefore has three primary operational SOP surfaces: this operating
contract, iterative delivery, and delivery governance. Gate 0 and handoff documents are
two specialist runbooks.

## Project Context

TrueAlpha is a fundamental and supply-chain research monorepo. It combines immutable raw
source capture, Postgres warehouse and knowledge-graph metadata, factor computation under
Dagster, and typed `mart` consumption through the Web App, MCP, and `/chat`. Current host
scripts are reconnaissance/bootstrap tooling, not scheduled release evidence.

Read `vision.md` for the investment questions and product rationale. Read `init.md` before
cross-service design, public contract, schema, release-gate, or known-risk decisions. Do
not load either document indiscriminately when a narrower source answers the task. Initial
reconnaissance findings and captured evidence live in
`apps/data-engine/samples/README.md`.

## Start And Resume Checkpoint

At the start of a task, after context compaction, and before resuming prior work:

1. Re-read the user's latest instruction. Do not treat a summary as newer authority.
2. Run `git status --short --branch` and preserve unrelated or user-owned changes.
3. Derive the workspace prefix from the checkout, never from the remote name:

   ```sh
   workspace_root="$(git rev-parse --show-toplevel)"
   workspace_name="$(basename "$workspace_root")"
   work_prefix="[$workspace_name]"
   ```

4. Declare the operating mode: issue coordination, owned delivery, review, or a
   user-named override.
5. Identify the exact issue, work key, PR, branch, writable paths, and read-only paths.
   Unknown ownership means read-only.
6. Search open and recently closed issues and PRs, remote branches, and batch manifests
   before creating work. Continue the existing work key; collapse duplicates promptly.
7. Read only the relevant authoritative documents and code. If `.codegraph/` exists,
   use CodeGraph before broad text search.
8. Before claiming repository implementation, run
   `make agent-preflight WORK_ISSUE=<number>`.

A clean-looking branch, matching title prefix, old session summary, or prior inspection
does not by itself authorize PR mutation.

## Operating Modes And PR Ownership

### Issue Coordination (Default)

Broad requests such as "review progress", "check the goal", "triage blockers", or
"continue" default to issue coordination. In this mode an agent may inspect repository
and GitHub state and create, edit, reprioritize, link, or close issues. Pull requests,
branches, review threads, CI controls, and merges remain read-only.

Issues are the live, shared coordination surface. Multiple agents may improve an issue's
problem statement, priority, dependencies, findings, owner, and next action. Treat issue
updates as cheap and immediate; do not wait for a code owner to record a newly verified
finding there.

### Owned Delivery

An agent may mutate a PR or its branch only when both gates pass:

1. The current task explicitly assigns the issue/work key or named PR to this agent or
   workspace for implementation, repair, rebase, review-thread cleanup, or merge.
2. The PR title's leading bracketed prefix exactly equals the checkout-derived
   `work_prefix`.

A matching prefix is necessary but not sufficient. Repository-wide responsibility,
issue edit permission, authorship of an earlier comment, or a request to review does not
grant delivery ownership. Write only the declared paths and work key.

### User-Named Override

The user may explicitly ask this agent to modify a named cross-prefix PR. That instruction
temporarily overrides the prefix gate for that PR and requested operation only. Record the
override in the context checkpoint; do not generalize it.

### Review And Handoff

For an unowned PR, inspect as needed but put actionable findings on its corresponding
issue and let the PR owner implement them. Do not push fixes, edit the PR body, reply to
or resolve threads, rerun or watch CI, rebase, close, or merge it unless the user grants a
named override.

For an authorized PR, independently verify every review thread. Resolve a thread directly
when its concern is fixed or demonstrably obsolete; unresolved verified threads must not
block delivery. Never resolve actionable, ambiguous, or unverified feedback.

## Issue-To-PR Contract

Every PR must correspond to exactly one issue. Create or repair the issue relationship
before implementation proceeds.

- Every PR body contains exactly one `Work-Issue: #<number>`, `Work-Key: <key>`, and
  `Issue-Action: <action>` field.
- Batch PRs use `<batch-id>:<target-rung>` and `managed-by-batch`.
- Standalone PRs use `standalone-<issue>` and either `complete-on-merge` or `keep-open`.
- Free-form mentions and GitHub closing keywords are not lifecycle authority.
- The issue records mutable coordination: current owner, priority, verified findings,
  dependencies, decisions, and next action.
- The checked-in batch manifest records immutable authorization and evidence inputs.
  Copying it into an issue body does not make the issue canonical.
- A PR owner reads the issue and applies changes to their PR. A coordination agent edits
  the issue rather than taking over the PR.
- An intermediate rung PR links the issue but does not close it. Close a capability issue
  only at its declared terminal evidence and after closure dependencies are accepted.

The unique work key is the issue or batch ID plus target rung, or an explicit standalone
task ID. Only one active issue ownership claim and one active PR may own a work key.

## Prefix, Deduplication, And Parallel Work

Owned issues and PRs use the exact title form
`<work_prefix> <plain English description>`. Do not put lane, batch, rung, stage, or task
tokens in titles; store them in manifests, labels, branches, or bodies. Shared root
capability and Gate issues remain unprefixed unless explicitly assigned.

Parallel agents may work only when work keys and writable paths are disjoint. There is one
active writer for shared graphs, migrations, registries, public exports, generated
conformance artifacts, root lockfiles, and authoritative documents. These surfaces require
the integration lease defined by the delivery SOP. Pending external authority or elapsed
evidence caps the claim; it does not justify taking another agent's work.

## Architecture Red Lines

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
  accepted sealed holdout/SLO policy.
- Every moomoo request goes through `api_call_ledger`; no module calls the API directly.
  The ledger is throttle and audit infrastructure, not a fictional monthly-call quota.
  The relevant quote/fundamental endpoints use burst rate limits; do not confuse a
  subscription tier ceiling with a call budget. See `init.md` Section 5.
- Moomoo access is Quote API read-only. Every trading context and every order placement,
  modification, cancellation, or trade-unlock operation is forbidden. The public
  repository's security CI must reject trading APIs rather than relying on review alone.
- Data-engine may hand strategy only ready capture evaluation, exact snapshots, and typed
  inputs. Consumers receive materialized outputs plus trace, usage, availability, catalog,
  universe, and release identities. Consumers pin accepted handoff IDs, never `latest`.

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
- `db/migrations/`: the schema source of truth for `raw`, `staging`, `mart`, and `dagster`.
- `db/roles.sql`: database role and permission configuration.
- `.github/workflows/`: GitHub Actions with path filtering; there is no moon setup.

## Evidence And Merge Boundary

Use `docs/iterative-delivery.md` for full rules. The claim ceilings are:

| Rung | Evidence | Maximum claim |
|---|---|---|
| E0 | Typed slice, lint/type/unit/negative checks | Experimental code |
| E1 | Immutable edge-case tiny run | Contract behavior |
| E2 | Classified repair and accepted pinned handoff | Stable local handoff |
| E3 | Immutable stratified medium run | Development candidate |
| E4 | Hardening, failure injection, freeze, independent holdout | Accepted candidate or bounded operation |
| E5 | Empty full build, capacity, recovery, real consumers, natural refresh | Large-scale shadow evidence |

E5 is not Production graduation. Gate acceptance is a separate fan-in over one exact
candidate, independent audits, complete terminal evidence, and human approval. Preserve
failed evidence. Semantic, PIT, schema, threshold, catalog, universe, or selection changes
invalidate dependent evidence; a post-freeze code change creates a new candidate.

`mergeable` means authorized and ready to merge now, not merely conflict-free. For the
exact head SHA, verify all of the following:

1. One non-duplicated authorized work key and valid path/lease ownership.
2. Compatible target-branch drift. Rebase only when intersecting drift invalidates code,
   inputs, generated artifacts, handoffs, or evidence; a disjoint `BEHIND` state alone is
   not a blocker.
3. Required acceptance and negative checks pass on the exact tested base/head pair.
4. Required review is current and every verified thread is resolved.
5. The PR's claim and issue action respect the evidence ceiling and terminal rung.
6. The result keeps `main` deployable, provisional capabilities disabled by default, and
   migrations backward compatible with rollback available.
7. Repository rules report no remaining block and no bypass is being used.

## Issue Quality Gate

Before creating or substantially changing an issue, inspect `vision.md`, `init.md`, the
current code/evidence, and existing issues/PRs. A task list without a causal argument is
not implementation-ready.

Every implementation issue contains:

1. **Problem context**: observed behavior, affected users/modules, evidence, scope, parent
   goal, and relevant code or artifact links.
2. **Root-cause analysis**: verified semantic, data, interface, or operational causes,
   clearly separated from hypotheses.
3. **Remediation**: changes mapped to causes, ownership boundaries, order, migrations,
   and explicit non-goals.
4. **Acceptance criteria**: executable positive, negative, PIT, replay, and operational
   evidence rather than proof that code was merely written.
5. **Why this completes the larger goal**: cause-to-change-to-evidence closure argument,
   downstream capability unblocked, residual risks, and remaining dependencies.

A capability implementation issue is not ready to start until its exact ID appears in a
versioned batch manifest, start dependencies are accepted, and earlier-rung inputs meet
their stated acceptance. Standalone repository operations use an explicit standalone work
key and do not claim a capability rung. Provisional work declares its evidence ceiling.
Exploration issues instead state competing hypotheses, evidence to collect, the enabled
decision, and a termination criterion.

## Context Compaction And Handoff

Before context compaction, agent handoff, or session end, write a concise checkpoint using
this exact structure. It combines Claude Code's current-state/error-correction emphasis
with Codex's exact-state resumption needs:

```md
# Session Title

## Objective And Latest User Intent
- Objective:
- Latest instruction:
- Explicit user corrections that must survive compaction:

## Mode And Authorization
- Mode: issue coordination | owned delivery | review | named override
- Owned issue and work key:
- Owned PR and branch:
- Exact override, if any:
- Writable paths/surfaces:
- Read-only or unowned paths/PRs:

## Authoritative Context
- Required docs and decisions:
- Architecture or product invariants:
- Exact handoffs/manifests/hashes:

## Current State
- Repository root, branch, base/head SHA, and worktree state:
- GitHub issue/PR/check/review state:
- Completed work:
- Files and symbols changed or inspected:

## Verification And Corrections
- Commands/evidence and results:
- Errors, failed approaches, and user corrections:
- Facts versus unresolved hypotheses:

## Next Actions
1. Immediate next action:
2. Remaining ordered actions:
- Blockers or external waits:
- Stop/termination condition:

## Do Not
- Unowned PR operations:
- Invalidated assumptions or approaches:
- User-owned/unrelated changes to preserve:
```

Compaction rules:

- Preserve the latest user correction even when it conflicts with an older plan.
- Record exact issue/PR numbers, work key, paths, SHAs, commands, evidence state, and next
  action. Do not write vague summaries such as "continue the goal".
- Separate completed, observed, inferred, blocked, and pending work.
- Record failed approaches so the resumed agent does not repeat them.
- A checkpoint preserves context but grants no authority. The resumed agent must rerun the
  Start And Resume Checkpoint and ownership gates before mutation.
- Never convert stale evidence, an unresolved hypothesis, or another agent's PR into owned
  work through summary wording.

## Environments And Source Gotchas

The target topology has Local, GitHub CI, Staging, and Production. This describes intent,
not proof that an environment or release gate is ready. Staging and Production are isolated
namespaced stacks; infra2 owns external Vault, MinIO, deployment, and promotion authority.
This repository consumes only released `infra2-sdk` contracts and never treats infra2 as a
TrueAlpha source dependency.

| Environment | Postgres | Object storage | Provisioning |
|---|---|---|---|
| Local | `make runtime-up` or localhost | Local MinIO | `make db-migrate`; bucket bootstrap |
| GitHub CI | Ephemeral service container | Ephemeral MinIO container | Per workflow run |
| Staging | `truealpha-postgres-staging`, host loopback `:15432` | Platform MinIO staging, bucket `truealpha-raw` | infra2 release promotion and `scripts/setup_vps_ingest.sh` |
| Production | `truealpha-postgres`, host loopback `:15433` | Platform MinIO, bucket `truealpha-raw` | Gate 4 shadow bootstrap, exact release manifest, explicit graduation |

- Current VPS host scripts and direct OpenD loopback access are reconnaissance/bootstrap
  only. They cannot satisfy scheduled-run or Staging/Production evidence. Issue #11 must
  provide an immutable data-engine/Dagster artifact and least-privilege OpenD boundary
  before scheduling counts.
- SEC XBRL concept tags and units vary across industries. Do not assume one field mapping
  works for every issuer.
- yfinance has no official SLA. Represent that limitation through lower row confidence;
  never make it a critical-path dependency or invent a provenance branch in factors.
- N-PORT holdings identify positions by CUSIP/ISIN, not ticker/CIK. Resolve identifiers
  through OpenFIGI or equivalent before writing PIT `same_as` KG edges. Do not recreate a
  flat `symbol_mapping` table in place of `staging.kg_entities`, `staging.kg_identifiers`,
  and `staging.kg_edges`.
- Build the structured-extraction primitive in `libs/factors/shared` before factor-specific
  gross-profit/headcount or pure-blood extraction. Do not duplicate extraction logic.

## Commands

- Install/check/test: `make install`, `make check`, `make test`.
- Local dependencies: `make runtime-up`, `make runtime-check`.
- Database: `make db-up`, `make db-migrate`.
- Python: `uv sync --all-packages`, `uv run pytest`, `uv run ruff check .`.
- Web: `cd apps/app-web && bun install`, `bun run dev`, `bun run typecheck`,
  `bun run build`.
- Governance: `make issue-graph-check`.
- Work claim: `make agent-preflight WORK_ISSUE=<number>`.

Reconnaissance/bootstrap ingestion is ordered and is never scheduled Gate evidence:

```sh
uv run --package truealpha-data-engine python apps/data-engine/scripts/bootstrap_universe.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_sec_facts.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/probe_moomoo_nonus.py
uv run --package truealpha-data-engine python apps/data-engine/scripts/sweep_moomoo_fundamentals.py --dry-run
```

The two moomoo commands require the OpenD host and
`MOOMOO_LEDGER_BACKEND=postgres`. Probe non-US endpoints before a full sweep.

Run the narrowest relevant tests first, then the declared manifest commands. Report what
was not run and why.
