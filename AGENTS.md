# TrueAlpha — Agent & Contributor Guide

> **Prohibition**: AI may NOT modify this file without explicit authorization.
> **Language**: All code, PRs, commits, and reports must be in **English**.
> **Authoritative architecture doc**: `init.md`. If this file and `init.md` disagree, `init.md` wins.

---

## 🚨 Red Lines (CRITICAL)

- **NEVER** commit `.env`, `*.pem`, or credential files.
- **NEVER** overwrite a point-in-time record — restatements insert new rows (`is_restatement`), never UPDATE.
- **NEVER** put computation logic outside `libs/factors` — the App layer does deterministic reformatting only (init.md Section 1, rule 2).
- **NEVER** let a factor branch on data provenance — factors see `(entity_id, value, confidence, as_of)` only.
- **NEVER** write staging rows without a `confidence` value.
- **NEVER** use float for monetary calculations where precision matters (DB columns are `numeric`).

## 🧱 Structure

- `apps/data-engine/` — Python (uv): ingestion → Postgres `raw` schema
- `apps/llm-service/` — Python (uv): FastAPI, MCP endpoint first, `/chat` Tier 3
- `apps/app-web/` — TypeScript (Bun): Next.js, reads `mart` directly
- `libs/factors/` — Python (uv): the seven modules; `base/` `composite/` `shared/`
- `db/` — plain SQL migrations for `raw`/`staging`/`mart`/`dagster` + `roles.sql`

No moon — CI is GitHub Actions with path filtering (`.github/workflows/`).

## 🔧 Commands

- `make install` / `make check` (lint + typecheck + test) / `make test`
- Python: `uv sync --all-packages`, `uv run pytest`, `uv run ruff check .`
- Web: `cd apps/app-web && bun run typecheck && bun run build`
- DB: `make db-up` (compose applies `db/` DDL on first boot)

## Delivery Batch Protocol

A GitHub release-gate milestone is one delivery batch. Exactly one gate epic may
carry `batch:active`; later gate epics carry `batch:queued`. The earliest
incomplete gate is the only batch that may be active. The product owner launches
the batch once by approving its manifest; agents then execute the whole batch
without requiring a separate prompt for each included issue. A gate epic cannot be
split across delivery batches.

1. **Freeze the batch before launch.** The active gate epic records the batch
   objective, exact included issue IDs, dependency frontier, non-goals, acceptance
   commands, required evidence, release/catalog/universe references when applicable,
   reviewer, and whole-batch rollback plan. The included IDs must equal the complete
   transitive open closure of the gate epic: every unfinished child required by its
   acceptance criteria and every unresolved blocker. A graph-parity check must prove
   that no gate-scoped work is omitted. This versioned record is the immutable batch
   manifest. No `batch:active` label means no implementation may start.
2. **Implement only frozen scope.** Code, schema, contract, authoritative-doc,
   pipeline, and acceptance-evidence work may start only for issue IDs enumerated
   in the active batch manifest. Milestone membership alone is not authorization.
   Issue PRs are focused review units; they must not merge independently into the
   default/protected branch or promote any environment.
3. **Parallelize preparation only.** Included issue PRs may be developed and
   reviewed in parallel when their dependency edges permit it. Their approved head
   SHAs are assembled in dependency order into one batch candidate; no issue PR is
   an independently mergeable, promotable, or releasable unit.
4. **Relaunch on scope change.** Adding or removing an issue, changing acceptance
   scope, or changing the dependency frontier after launch cancels the candidate
   and invalidates its evidence. The product owner must publish a new manifest and
   relaunch the same earliest incomplete gate. A cancelled candidate cannot merge
   or promote, and removing failed scope narrows the claim rather than making it pass.
5. **Deliver one immutable candidate.** The complete set of approved issue-PR head
   SHAs produces one candidate commit and one release/artifact content hash. All
   integration checks, review, merge, and promotion bind that exact candidate.
   Any candidate change invalidates prior evidence. Exactly one batch PR may merge
   the candidate into the default/protected branch, and every required environment
   receives that same complete candidate, never a subset or rebuilt variant.
   Rollback applies to the whole candidate.
6. **Close after coherent delivery.** Every included issue's executable acceptance
   criteria, the gate epic's complete acceptance criteria, batch-level integration/
   negative/replay checks, evidence links, documentation, and dependency graph must
   agree before reviewer approval. The parity check must still show no open gate-
   scoped issue or blocker outside the manifest. The batch closes only after its
   single batch PR merges and every required whole-candidate promotion or graduation
   succeeds. A blocked batch remains active until cancelled and relaunched.
7. **Activate the next batch explicitly.** Only after the current batch closes may
   the product owner move `batch:active` to the next gate epic. An emergency hotfix
   must become the sole active batch: cancel the current launch, complete the
   hotfix batch, then relaunch the interrupted gate from a new manifest. Emergency
   work cannot overlap with or be folded retroactively into an active batch.
8. **Enforce one default-branch entry point.** Every included issue PR must target
   the manifest's batch integration branch or remain unmerged as an approved head
   SHA. It must never target the default/protected branch. The aggregate batch PR,
   whose head is the immutable candidate commit, is the only PR permitted to target
   that branch. Direct pushes, cherry-picks around the candidate, and a second batch
   PR for the same launch are forbidden.
9. **Close scope atomically.** Included issues remain open until the aggregate batch
   PR and every required whole-candidate promotion have succeeded. Then the included
   issues, gate epic, and milestone close from the same evidence bundle, with the
   epic closing last. Cancellation, failed promotion, or whole-candidate rollback
   leaves or returns every included issue to open state; an approved child PR or a
   green issue-level check is not issue completion.

Within frozen scope, agents continue across child issues without waiting for a new
prompt at each boundary. They escalate only a real product decision, security/legal
risk, required relaunch, or blocker that cannot be resolved inside the manifest.

Every issue PR, batch PR, commit report, progress report, and completion report must
name the active batch-manifest version and the acceptance evidence it advances. One
final completion report is issued for the batch as a whole.

## Issue Quality Gate

Every issue must explain a complete causal path from the observed problem to the
project goal. A task list without that argument is not ready for implementation.
Before creating an issue, check `vision.md`, `init.md`, existing issues, and the
current code or evidence so the proposal does not duplicate work or contradict
the authoritative architecture.

An implementation issue is not ready to start unless its exact issue ID is
enumerated in the immutable active batch manifest and all dependencies are already
accepted or included in that manifest. Milestone assignment alone is insufficient,
and only the aggregate batch PR may merge into the default/protected branch.

Every implementation issue must contain these sections:

1. **Problem context** — Describe the observed behavior, affected users or
   modules, current evidence, scope, and the larger goal that is blocked. Link
   the relevant `vision.md` / `init.md` phase, parent issue, code, data, or run.
2. **Root-cause analysis** — Explain why the problem exists at the semantic,
   data, interface, or operational boundary. Distinguish verified causes from
   hypotheses. Do not restate the symptom as the cause.
3. **Remediation** — Specify the proposed changes, ownership boundaries, data or
   interface migrations, implementation order, and explicit non-goals. Each
   change must address a named root cause.
4. **Acceptance criteria** — Use observable, executable outcomes wherever
   possible: tests, queries, quality gates, replay assertions, workflow runs, or
   artifacts. Cover negative and point-in-time cases, not only the happy path.
5. **Why this completes the larger goal** — Provide the closure argument:
   map root causes to changes, changes to acceptance evidence, and that evidence
   to the downstream capability that becomes unblocked. List residual risks,
   dependencies, and follow-up work; if any dependency still blocks the stated
   goal, narrow the issue's claimed outcome instead of declaring completion.

Exploration issues may begin with an unverified root cause, but must state the
competing hypotheses, the evidence to collect, the decision that evidence will
enable, and a termination criterion. They must result in either a verified
implementation issue or a documented decision that no change is required.

An issue is not ready when acceptance criteria only confirm that code was
written, when evidence can be satisfied by manually flipping a flag, or when
the proposed work does not prove which downstream blocker it removes.
