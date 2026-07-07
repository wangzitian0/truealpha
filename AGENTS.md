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
