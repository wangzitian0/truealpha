<!-- WS_STATIC_START adapter=rules-v2 inputs=f021d7a6bbe07817accbb8a1ce7fea7608a8b17c3ff104ad0ab07fccaf52f741 -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Before an abstract architecture proposal, inspect the system with read-only probes such as `time cmd`, process chains, and file-descriptor locks. A conceptually neat story without physical evidence is insufficient. A probe must not write. Do not combine validation and action in one command: a POST permission probe can create a resource, and a trial commit can leave a real commit. Measure, read the result, then decide, with a stop point between these steps.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Do not hide assertions in `if (exists)` or `if (code != 0)` so failures run zero assertions. Do not accept tautologies such as `typeof null === 'object'` or `result !== undefined || true`. Source-text `indexOf` matches against prose are not integration tests. Duplicate test function identifiers can silently shadow earlier tests (Python `def` and duplicate JS function/const/export names); duplicate string titles in `test()` or `it()` instead run both. A green count is not coverage evidence. Make a new test fail under a relevant mutation. A read-only reviewer treats such fake-test patterns as CRITICAL merge blockers.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications in concrete ASD-STE100 terms without vague qualifiers.
- **One source of truth:** Keep one authoritative definition per core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.
- **Physical context verification:** Static file existence does not prove host runtime context injection. Junior runs non-interactive probes (`-p` or headless exec) with low-cost models, asserting visible specification text across root and deep subdirectories.
- **Host verification scope:** Official host CLIs are the primary execution environment. Regular testing covers Antigravity 2.0 project workspaces (`.agents/skills/` and project rules); standalone desktop chat apps without directory awareness are excluded.
- **Local and remote test segregation:** Inner-loop verification (Senior, Junior, Intern) runs only focused tests on changed files (`pytest <file>::<test> -x`). Full-suite regression runs on remote CI, or locally at most once before merge.
- **Bounded file reads:** Pass `StartLine` and `EndLine` (`<= 120` lines per call) when reading existing files with `view_file`.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write all committed artifacts, rule definitions, source code, inline comments, commit messages, and documentation in English using the ASD-STE100 (Simplified Technical English) specification. Keep sentences short: maximum 20 words for instructions and maximum 25 words for descriptions. Use the active voice. Use approved technical words with one meaning per word. Express one topic per sentence. Do not use ambiguous qualifiers (such as "properly", "efficiently", or "seamlessly"). Operational skills and engineering playbooks (`skills/**/SKILL.md`) that contain interactive procedures follow the target repository or author team's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread directly after independently verifying it is fixed or obsolete. Do not resolve actionable, ambiguous, or unverified feedback. Automated reviewers may read a redacted GitHub diff rather than source: GitHub can show `"Authorization": f"Bearer ******"` where source has `"Authorization": f"Bearer {token}"`. Check source before judging a report. When a report is false, turn the concern into a falsifiable invariant test rather than merely dismissing it.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.
- **Delivery state invariant:** Never declare complete, done, or finished while a PR is unmerged or uncommitted. Never declare complete when Stage 1 (Merge to main) has landed without a verified Business Reality Probe. Code landing on main is strictly 'Code Landed'. If a PR is open, report state strictly as 'In review' with the PR URL. If uncommitted or local only, report 'Draft' or 'Local verified'. If merged without a reality probe, report 'Stage 1 Landed'.
- **Anti-exclusion and reality invariant:** Exclusion is not resolution. Marking failing entities as excluded, adding expiring exemptions, or filtering out edge cases to manufacture green checks is a defect. A task is complete only when a physical reality probe confirms non-empty real-world execution.
- **Three-stage deployment prerequisite:** Stage 1 (Merge to main) is a non-bypassable physical prerequisite for Stage 2 and Stage 3. If HEAD is not on main, fail closed and refuse any deployment request.
- **Deploy provenance invariant:** Before dispatching any deploy workflow, physically verify that the target ref or tag contains current branch commits (`git merge-base --is-ancestor HEAD <target>`). Never deploy an older tag as completion evidence for current work.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores. Examples: a deploy, promote or apply to production; a production secret write; a manual production data write or deletion. Also a production DNS, route or host change, and a release that cannot be recalled. Code that reaches production with no later owner approval, for example on a schedule, counts too. Each Repo tier names its production paths.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner. Examples: the owner-approval check of a deploy path, a production job classification, the settings of a production environment. It also includes an edit that removes or weakens a pin of this section or its dev_env pin test. The same holds for the required check that runs the test.
- **Everything else is the agent's:** in a repository that the owner owns, every merge, staging and canary deploy, and release tag. Also rule text, skills, gate code, data and tests, required-check workflows and release code. Before you merge gate or release code, a read-only reviewer that did not write it checks the head. The review states whether the gate gets stricter, stays equal or gets looser. A looser edit in the self-guard list follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate exit code that holds non-production work for the owner. Do the work, then fix the hold. The dev_env check `ws_owner_scope` fails on it.

## Runtime safety

- **Reason from the worst case.** For environment changes, wrappers, redirection, or interception rules, check for no-TTY deadlock in CI or child processes, concurrent shared-file truncation/races, and network or cold-start failure cascades. Reject a proposal whose lack of backlash cannot be established.
- **Treat three hidden green failures as defects:** WRONG FORMULA (an incorrect formula passes assertions), GREEN-WHILE-EMPTY (filtering removes all output but reports success), and STALE-REPORTED-AS-FRESH (old data is labeled fresh). Implausible output is evidence of a defect.
- **Protect ambient services.** Default unit tests and Junior tasks must not destructively act on host ports, shared background processes, or development databases (`DROP`, `TRUNCATE`, `--clear`, forced restart). Resets require an isolated sandbox/worktree with a dedicated random port, or an explicit `CI=true` or `ALLOW_CLEAR_TEST=1` guard; otherwise skip safely with a warning.
- **Two triggers:** Every background job or batch process needs both scheduled execution and manual replay.
- **Do not steal CD locks:** A trigger is instant but publication is delayed. Do not interrupt a running deployment; the next run must coalesce commits accumulated while it was busy.

# TrueAlpha Agent Contract

> **Rule source**: This file is generated. Change its dev_env source and pass the rule-text checklist; do not hand-edit it.
> **Repository language**: Code, commits, branches, pull requests, and issues are written
> in English. User-facing conversation may follow the user's language.
> **Architecture authority**: `init.md` wins on architecture and public contracts;
> `vision.md` wins on product scope. `CLAUDE.md` is a symlink to this file and
> `GEMINI.md` delegates here.

## How work happens

1. **One issue, one PR.** Every PR references its issue (`Closes #N` when it completes
   the issue, a plain `#N` reference otherwise). Keep PRs small and reviewable.
2. **Issues are the shared coordination surface.** Record verified findings, decisions, and
   next actions on the issue as you learn them.
3. **Parallel agents** work on disjoint files. Prefix issue and PR titles with your workspace
   name (for example `[truealpha-factors]`). If two lanes need the same file, coordinate through
   the issue instead of racing. Shared surfaces (migrations, registries, public exports,
   lockfiles) deserve a heads-up comment first. File a finding outside your lane with its evidence
   and hand it to the owning lane: recording it is in scope, adopting it is not.
4. **Before starting**: `git status --short --branch`, sync `main`, check open issues and
   PRs for the same work. **Strong dashboards, weak gates**: Merge gates enforce baseline
   correctness (compilation, type checks, backward-compatible migrations, secrets scanning,
   focused tests, deployable `main`). Third-party and AI reviewer feedback is advisory: reviewer
   platform errors, timeouts, and unparseable reviews warn and never block. Actionable review
   findings are tracked, with unresolved blockers budgeted (High = 0, Medium <= 2, Low <= 4). Live dashboards (`/admin/datahub`,
   `/research/backtest`, Postgres `mart` health tables) govern observability, not regexes on
   PR descriptions or issue comments. Once a PR is merge-ready, the agent that owns it merges
   it. That covers gate code, workflows and release code once the review that the Root section
   `Production reservation` requires is on the PR. Production here (`docs/release-protocol.md`):
   `tools/cut_release.sh <tag> --prod` and `deploy-release.yml` with `deploy_type=prod`. The
   checks of `--owner-approved-sha` and `owner_approved_sha` in those paths are part of the
   reservation itself.
5. **Data and evidence stay verifiable.** Captured corpora, snapshots, handoff records,
   and evaluation evidence carry content hashes so a replay provably uses the same bytes.
   Records live under `governance/` (see its README); they document what happened and are
   never a merge gate.
6. **Closed means deployed, real, and evidenced.** A product issue closes only when a deployed
   path invokes its capability on real captured data. A deployed path is reachable from the
   Dagster composition root (`dagster_defs.py`) or a deployed service/App entrypoint. Post
   evidence on the issue: the deployed call site plus real-data output (SQL rows or an HTTP
   response). Reaching a read model is not reaching the reader: where a capability has an
   owner-facing surface, name the page or response field that renders it (#284). Fixture data lives
   in tests only; fixture assets are named `*_fixture` and are never scheduled or reachable
   from a deployed route. Code that merely exists is not done: wire it into the deployed path
   or explicitly demote it on the issue.
   (vision.md; drift audit #429, invariants I1–I4; root #434.) Landing on main via PR merge gate is a prerequisite before deployment. Do not trigger deployment from an unmerged branch.
   Honest partial scope stays open with a scope note instead of closing.
7. **Acceptance criteria are standing checks, and they cover the whole scope.** See
   `docs/architecture-decisions/A2-acceptance-criteria-are-standing-checks.md` (#371, #494, #495):
   - **Every scope item has a matching acceptance criterion.** If the issue body names a
     deliverable, exactly one criterion must assert it. Delete deliverables without a check,
     or write the check. Judge closure against scope item by item.
   - **A criterion is a check that runs again, not a run that happened.** Pasted output is
     evidence. Acceptance is a named CI step, test, or gate that turns red on the next
     regression. A one-time manual walk or script satisfies rule 6 and still fails this rule.
     State where the check lives.
   - **A criterion must be able to fail where production calls.** A test that supplies an
     argument the deployed caller omits proves the parameter, not the wiring. Assert through
     the deployed entry point. Confirm the check fails on unfixed code first.
   `Closes #N` asserts all three rules. When a merge cannot prove a criterion (such as deployed
   state), use a plain `#N` and close by hand with a comment walking the criteria.

Before editing or after compaction, re-read the prompt, run rule 4 checks, and note the issue,
branch, and target files. On handoff, post done, verified, next, and failed items on the issue.

## Project context

TrueAlpha is a fundamental and supply-chain research monorepo: raw source capture, a Postgres
warehouse with knowledge-graph metadata, Dagster factors, and typed `mart` consumption through the
Web App, MCP, and `/chat`. Read `init.md` before cross-service design, public contract, schema, or
known-risk decisions. Reconnaissance findings live in `apps/data-engine/samples/README.md`.

Repository shape:

- `apps/app-web/`: TypeScript/Next.js; reads `mart` through a read-only account.
- `libs/factors/base/`: provenance-neutral PIT factors; modules 1-6.
- `libs/factors/composite/`: factors that reload materialized upstream outputs; module 7.
- `libs/factors/shared/`: KG entity resolution and the shared structured-extraction
  primitive. Do not reimplement extraction per factor.
- `db/migrations/`: the schema source of truth for `raw`, `staging`, `mart`, `dagster`,
  and `app`.
- `governance/`: historical delivery records (capabilities graph, evidence, handoffs).

## Architecture red lines

- Never commit `.env`, `*.pem`, tokens, credentials, account identifiers, private hosts,
  or secrets in code, fixtures, comments, or docs. Redact live-session output before it
  reaches a tracked file. Secret scanning is a backstop, not permission.
- Point-in-time data distinguishes `valid_time` from `transaction_time` (knowable-at).
  Write `transaction_time` explicitly from a source property, never an insertion-clock
  default. `recorded_at` is ingestion audit time only.
- A constant never stands in for a measurement. A run's shape (universe size, listing and
  obligation counts), a record's time, and a field's freshness are derived from the run or
  the source, never written down as a literal or a default. This is the most repeated
  defect shape here; each instance passes every test built on the founding
  assumption: an insertion-clock `transaction_time`, a stamped `freshness`, a hardcoded
  84-cell denominator (#539).
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
- LLM extraction is a versioned, append-only step. Bind model, instructions, schema, and
  decoding settings; store semantic results and evidence spans. Replay never calls a model.
  Confidence uses the continuous `0-100` score (`prior x agreement x freshness x lineage`), never
  a constant. Single-source extraction lowers confidence and never blocks a row.
- Every external request (SEC, Twelve Data, yfinance, OpenFIGI, N-PORT, moomoo, the LLM
  extraction provider) goes through the source gateway and `api_call_ledger` with a declared
  capacity. No module calls a vendor or a model directly (init.md rule 6). The ledger is
  throttle and audit infrastructure, not a monthly-call quota: moomoo's quote/fundamental
  endpoints use burst rate limits; do not confuse a tier ceiling with a call budget.
- Moomoo access is Quote API read-only. Every trading context and every order placement,
  modification, cancellation, or trade-unlock operation is forbidden. Security CI rejects
  trading APIs rather than relying on review alone.
- Consumers of data-engine outputs read through `mart.served_head` (or a pinned
  `run_id` / `snapshot_id` / `evidence_hash`). A bare mutable `latest` is never a read path.
  The pointer advances only on an accepted refresh, and consumers re-pull when it advances
  (`docs/architecture-decisions/A1-evidence-chain-in-database.md`).

## Environments and source gotchas

Target topology: Local, GitHub CI, Staging, Production. Staging and Production are
isolated namespaced stacks; infra2 owns external Vault, MinIO, deployment, and promotion.
This repository consumes only released `infra2-sdk` contracts.

| Environment | Postgres | Object storage | Provisioning |
|---|---|---|---|
| Local | `make runtime-up` or localhost | Local MinIO | `make db-reset`, `make db-migrate`, `make db-check` (see Commands); bucket bootstrap |
| GitHub CI | Ephemeral service container | Ephemeral MinIO container | Per workflow run |
| Staging | `truealpha-postgres-staging`, host loopback `:15432` | Platform MinIO staging, bucket `truealpha-raw` | infra2 release promotion and `apps/data-engine/scripts/setup_vps_ingest.sh` |
| Production | `truealpha-postgres`, host loopback `:15433` | Platform MinIO, bucket `truealpha-raw` | Explicit graduation |

- Current VPS host scripts and direct OpenD loopback access are reconnaissance/bootstrap
  only; they are not scheduled-run evidence.
- SEC XBRL concept tags and units vary across industries. Do not assume one field mapping
  works for every issuer.
- `yfinance` is the zero-quota default base for price and split coverage. Twelve Data and
  moomoo corroborate within their quotas. Represent `yfinance`'s missing SLA through lower
  confidence (`Low` when single-source), never a provenance branch in factors. Price reuse
  keys on `(listing, session_date)`, not issuer namespace.
- N-PORT holdings identify positions by CUSIP/ISIN, not ticker/CIK. Resolve identifiers
  through OpenFIGI or equivalent before writing PIT `same_as` KG edges. Use
  `staging.kg_entities`, `staging.kg_identifiers`, and `staging.kg_edges`, not a flat
  symbol-mapping table.

## Commands

- Install/check/test: `make install`, `make check`, `make test`.
- Database: `make db-up`, `make db-migrate` (apply the chain forward), `make db-reset`
  (drop, recreate, re-apply — the repair path when a database has drifted; replay does not
  repair a table already in a superseded shape), `make db-check` (diff a live database
  against the declared chain).

Reconnaissance/bootstrap ingestion runs these scripts of `apps/data-engine/scripts/` in order
(`uv run --package truealpha-data-engine python <script>`): `bootstrap_universe.py`,
`sweep_sec_facts.py`, `probe_moomoo_nonus.py`, `sweep_moomoo_fundamentals.py --dry-run`. The
moomoo scripts need the OpenD host and `MOOMOO_LEDGER_BACKEND=postgres`. Probe non-US endpoints
before a full sweep.

Run the narrowest relevant tests first. Report what was not run and why.
<!-- WS_STATIC_END -->
