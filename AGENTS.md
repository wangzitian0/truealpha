<!-- WS_STATIC_START adapter=rules-v2 inputs=714f4589795c50a326046b0e1e6151ec1f575115e3e1f3202d3d52bfac935510 -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Before an abstract architecture proposal, inspect the system with read-only probes such as `time cmd`, process chains, and file-descriptor locks. A conceptually neat story without physical evidence is insufficient. A probe must not write. Do not combine validation and action in one command: a POST permission probe can create a resource, and a trial commit can leave a real commit. Measure, read the result, then decide, with a stop point between these steps.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Do not hide assertions in `if (exists)` or `if (code != 0)` so failures run zero assertions. Do not accept tautologies such as `typeof null === 'object'` or `result !== undefined || true`. Source-text `indexOf` matches against prose are not integration tests. Duplicate test function identifiers can silently shadow earlier tests (Python `def` and duplicate JS function/const/export names); duplicate string titles in `test()` or `it()` instead run both. A green count is not coverage evidence. Make a new test fail under a relevant mutation. A read-only reviewer treats such fake-test patterns as CRITICAL merge blockers.
- **One source of truth:** Keep one authoritative definition for each core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.

## Delivery and merge

- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread directly after independently verifying it is fixed or obsolete. Do not resolve actionable, ambiguous, or unverified feedback. Automated reviewers may read a redacted GitHub diff rather than source: GitHub can show `"Authorization": f"Bearer ******"` where source has `"Authorization": f"Bearer {token}"`. Check source before judging a report. When a report is false, turn the concern into a falsifiable invariant test rather than merely dismissing it.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.

## Runtime safety

- **Reason from the worst case.** For environment changes, wrappers, redirection, or interception rules, check for no-TTY deadlock in CI or child processes, concurrent shared-file truncation/races, and network or cold-start failure cascades. Reject a proposal whose lack of backlash cannot be established.
- **Treat three hidden green failures as defects:** WRONG FORMULA (an incorrect formula passes assertions), GREEN-WHILE-EMPTY (filtering removes all output but reports success), and STALE-REPORTED-AS-FRESH (old data is labeled fresh). Implausible output is evidence of a defect.
- **Protect ambient services.** Default unit tests and Executor tasks must not destructively act on host ports, shared background processes, or development databases (`DROP`, `TRUNCATE`, `--clear`, forced restart). Resets require an isolated sandbox/worktree with a dedicated random port, or an explicit `CI=true` or `ALLOW_CLEAR_TEST=1` guard; otherwise skip safely with a warning.
- **Two triggers:** Every background job or batch process needs both scheduled execution and manual replay.
- **Do not steal CD locks:** A trigger is instant but publication is delayed. Do not interrupt a running deployment; the next run must coalesce commits accumulated while it was busy.

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
   you touch them. A finding outside your lane is filed with its evidence and handed to
   the owning lane — recording it is in scope, adopting it is not.
4. **Before starting**: `git status --short --branch`, sync `main`, check open issues and
   PRs for the same work. The configured code-review process is every reviewer required by
   repository settings or explicitly requested on the PR. It is complete only when each has
   submitted a review for the exact head SHA. An actionable finding is an unresolved review
   comment that requests a concrete code, documentation, test, or process change; questions
   and informational comments do not count. Before declaring a PR merge-ready, require that
   process to complete; an empty thread list before completion is not evidence of a clean
   review. For each reviewer, only their latest effective decision counts; a latest
   `Changes requested` decision blocks readiness until a later approval or dismissal, even
   when it created no review thread. Each actionable finding must be classified in its own
   review thread as exactly `Severity: High`, `Severity: Medium`, or `Severity: Low`; a
   classification only in a review summary does not count. An unclassified actionable
   finding blocks readiness.
   Evaluate unresolved actionable findings against the budget of High = 0, Medium <= 2, and
   Low <= 4 on the exact head immediately before merge readiness is declared. A green
   `ci-required`, a deployable `main`, and backward-compatible migrations are also required.
   Once a PR is merge-ready, the agent that owns it merges it. Only a merge whose pipeline
   reaches production (a release promote, see `docs/release-protocol.md`) waits for the
   owner's approval of that exact head SHA.
5. **Data and evidence stay verifiable.** Captured corpora, snapshots, handoff records,
   and evaluation evidence carry content hashes so a replay provably uses the same bytes.
   Records live under `governance/` (see its README); they document what happened and are
   never a merge gate.
6. **Closed means deployed, real, and evidenced.** A product issue closes only when its
   capability is invoked by a deployed path — reachable from the Dagster composition root
   (`dagster_defs.py`) or a deployed service/App entrypoint — on real captured data, with
   evidence posted on the issue: the deployed call site plus real-data output (SQL rows or
   an HTTP response). Reaching a read model is not reaching the reader: where a capability
   has an owner-facing surface, name the page or response field that renders it (#284: PEG
   reached `mart` and both read repositories while no page referenced it). Fixture data
   lives in tests only; fixture assets are named `*_fixture` and are never scheduled or
   reachable from a deployed route. Code that merely exists is not done: wire it into the
   deployed path or explicitly demote it on the issue. Honest partial scope stays open with a scope note instead of closing.
   (vision.md: fixture-only tools or code existence are not completion evidence;
   drift audit #429, invariants I1–I4; root #434.)
7. **Acceptance criteria are standing checks, and they cover the whole scope.** Two rules,
   both learned from closed issues whose scope silently evaporated (#371, #494, #495 —
   see `docs/architecture-decisions/A2-acceptance-criteria-are-standing-checks.md`):
   - **Every scope item has a matching acceptance criterion.** If a deliverable is named in
     the issue body, exactly one acceptance criterion must assert it. A deliverable with no
     criterion does not belong in the scope — delete it or write the check. Closure is
     judged against the scope, item by item, not against whichever criteria happen to exist.
   - **A criterion is a check that runs again, not a run that happened.** "I executed this
     and pasted the output" is evidence; acceptance is a named CI step, test, or gate that
     turns red on the next regression. A one-time manual walk, a hand-run script, or a
     measurement taken on one production run satisfies rule 6's evidence requirement and
     still fails this one. State how the check is armed and where it lives.
   - **A criterion must be able to fail where production calls.** A test that supplies an
     argument the deployed caller omits proves the parameter, not the wiring — it is green
     *because* it supplies what production does not. Assert through the deployed entry
     point, and confirm the check goes red against the unfixed code before trusting it
     (#284: two parser vintages computed nothing in either environment while every gate,
     including the end-to-end test written to prove the path, stayed green).
   Auto-closing an issue with `Closes #N` asserts all three rules were evaluated. When an
   issue carries a criterion a merge cannot prove — a user journey, a role-dependent view, a
   deployed-environment state — reference it with a plain `#N` instead and close it by hand
   with a comment that walks the criteria in order.

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
- A constant never stands in for a measurement. A run's shape (universe size, listing and
  obligation counts), a record's time, and a field's freshness are derived from the run or
  the source, never written down as a literal or a default. This is the repository's most
  repeated defect shape, and each instance passes every test built on the founding
  assumption: an insertion-clock `transaction_time`, a stamped `freshness`, a hardcoded
  84-cell denominator that survived five serial fixes (#539), a growth window behind a
  default-off flag (#284).
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
- Every external request — SEC, Twelve Data, yfinance, OpenFIGI, N-PORT, moomoo and the
  LLM extraction provider — goes through the source gateway and `api_call_ledger` with a
  declared capacity; no module calls a vendor or a model directly (init.md rule 6, owner
  decision 2026-09-04). The ledger is throttle and audit infrastructure, not a fictional
  monthly-call quota: moomoo's quote/fundamental endpoints use burst rate limits; do not
  confuse a subscription tier ceiling with a call budget. See `init.md` Section 5.
- Moomoo access is Quote API read-only. Every trading context and every order placement,
  modification, cancellation, or trade-unlock operation is forbidden. The public
  repository's security CI must reject trading APIs rather than relying on review alone.
- Consumers of data-engine outputs read through a governed, access-controlled
  `current_pointer` that resolves to an immutable exact run, identified by that run's
  content hash. Pinning an exact snapshot/handoff identity for a reproducible read stays
  supported; a bare mutable `latest` is never a read path. The pointer advances only on an
  accepted refresh, and consumers re-pull when it advances. See
  `docs/architecture-decisions/A1-evidence-chain-in-database.md`.

## Environments and source gotchas

Target topology: Local, GitHub CI, Staging, Production. Staging and Production are
isolated namespaced stacks; infra2 owns external Vault, MinIO, deployment, and promotion.
This repository consumes only released `infra2-sdk` contracts.

| Environment | Postgres | Object storage | Provisioning |
|---|---|---|---|
| Local | `make runtime-up` or localhost | Local MinIO | `make db-reset` to start where CI starts, `make db-migrate` to move forward, `make db-check` to prove it matches; bucket bootstrap |
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
- Database: `make db-up`, `make db-migrate` (apply the chain forward), `make db-reset`
  (drop, recreate, re-apply — the repair path when a database has drifted; replay does not
  repair a table already in a superseded shape), `make db-check` (diff a live database
  against the declared chain).
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
<!-- WS_STATIC_END -->
