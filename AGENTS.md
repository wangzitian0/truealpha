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

## Issue Quality Gate

Every issue must explain a complete causal path from the observed problem to the
project goal. A task list without that argument is not ready for implementation.
Before creating an issue, check `vision.md`, `init.md`, existing issues, and the
current code or evidence so the proposal does not duplicate work or contradict
the authoritative architecture.

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
