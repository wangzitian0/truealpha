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

## Iterative Capability Delivery Protocol

Release-gate milestones define product claims and promotion order. They are not
implementation batches. A capability batch owns one bounded vertical slice across an
explicit contiguous range of the data-application loop. Each batch PR advances that
slice by exactly one rung, and a gate may contain many independently mergeable batches.
Passing a lower rung never implies that a higher rung or gate passed.

The evidence ladder is:

1. **E0 Code** - Build the smallest typed vertical slice. Run lint, type, unit, and
   negative contract checks. APIs remain experimental and no data-readiness claim is
   allowed.
2. **E1 Tiny** - Run end to end on a content-hashed, predeclared tiny corpus chosen to
   expose semantic branches. Where applicable include success, missing/unavailable,
   revision/restatement, and cutoffs on both sides of public availability. Prove PIT
   exclusion, append-only writes, deterministic replay, lineage, and fail-closed errors.
3. **E2 Contract repair** - Classify every tiny-run finding as a local bug, generic
   toolkit gap, semantic ambiguity, or data/source issue. Version generic contract or
   schema changes, add compatibility and negative tests, rerun E1, and publish an exact
   content-hashed handoff.
4. **E3 Medium** - Run the pinned handoff over an immutable stratified denominator with
   multiple subjects, cutoffs, vintages, regimes, and declared failure states. Prove no
   denominator shrink, fixture/Postgres parity, replay equality, reconciliation, SLO
   calculation, and resource telemetry. This is development evidence, not a holdout.
5. **E4 Harden and freeze** - Meet predeclared latency, throughput, memory, storage,
   cost, and headroom budgets. Inject partial writes, retries, rate limits, crashes,
   schema drift, and out-of-order inputs as applicable. Freeze exact code, configuration,
   contract, catalog, universe, and threshold hashes before an independent holdout. A
   bounded Staging canary may prove operation for that exact candidate, but not full-scope
   capacity, natural refresh, or Production readiness.
6. **E5 Large and shadow** - Build an empty isolated database/object store from the
   approved release and full scope. Prove row completeness, capacity, non-starvation,
   recovery/checksums, rights/budget validity, real consumers, and observed post-freeze
   natural source transitions. Fixture replay, retries, unchanged bytes, reparsing, and
   synthetic mutation never count as natural refresh.

Gate acceptance and Production graduation are a separate fan-in state, not an evidence
rung. E5 can supply shadow evidence but cannot close a gate by itself. Graduation requires
the exact candidate's complete gate bundle, independent capture audit, final Vision audit,
and recorded human approval.

Every implementation batch follows these rules:

1. **Freeze a narrow manifest.** The canonical manifest is a checked-in, content-hashed
   artifact under `governance/batches/`; a mutable issue body is never the source of truth.
   Record the batch ID/revision, capability issue IDs, last-accepted/current-target/
   terminal rung, objective, claim and readiness ceiling, base SHA, exact input handoff
   IDs/hashes, dependency class, corpus/denominator, expected outputs,
   acceptance and negative commands, owners/reviewers, non-goals, invalidation triggers,
   rollback, and writable, read-only, and forbidden path globs. Holdout manifests record
   custody and protocol, never protected labels or results. `batch:*` labels belong only
   to batch-manifest issues, never release-gate epics or capability issues.
2. **Use lanes and exclusive path ownership.** The primary lanes are capture/storage,
   orchestration/platform, strategy, consumption, and verification/operations.
   Contracts/toolkit is not an independent execution lane: it is a shared integration
   surface. Up to five work packets may run concurrently when their writable paths are
   disjoint and their consumed handoffs are already merged. There is one active writer per
   path and one integration lease for shared types/exports, registries, migrations,
   generated conformance artifacts, root lockfiles, and authoritative docs.
3. **Classify dependencies by the claim they block.** A start dependency blocks even
   provisional implementation; a freeze dependency permits fixture work but blocks a
   stable candidate; a closure dependency permits a frozen candidate but blocks issue or
   gate acceptance. After an accepted E2 handoff merges, downstream agents may implement
   against that exact ID without waiting for the whole release gate. Earlier fixture
   prototypes are allowed only when the manifest marks them provisional. Consumers pin
   exact handoffs and never resolve `latest` or infer policy.
4. **Keep source and computation boundaries strict.** Data-engine hands strategy only a
   ready capture evaluation, exact snapshot, and typed inputs. Strategy never reads raw
   rows, vendor identity, rights, or source priority. Consumers receive only materialized
   outputs plus trace, usage, availability, catalog, universe, and release identities.
5. **Merge small verified increments.** One PR accepts exactly the batch's current target
   rung and names its manifest hash and handoffs. It rebases on current `main`, passes rung-specific
   checks, advances the target by at most one rung, and may merge independently.
   Dependency-topological merge order and the integration lease serialize shared paths.
   A stage merge never promotes an environment
   or claims gate completion. After rebase, update the manifest base/revision/hash and issue
   link, then rerun all affected evidence; stale evidence cannot merge. `main` remains deployable:
   provisional code is absent from the accepted `ReleaseManifest` registry/configuration
   bindings and is not registered, scheduled, routed, or selected by default. Its
   migrations are additive and backward
   compatible, and a negative release test proves an unaccepted capability cannot run.
6. **Freeze before observing protected results.** Formula semantics, sampling strata,
   thresholds, minimum sample counts, custody/conflict rules, source/knowability policy,
   applicability, and SLO denominators freeze before medium evaluation or holdout reveal
   as applicable. Any evaluated-code or policy change after freeze creates a new version
   and requires a fresh untouched holdout.
7. **Respect the evidence ceiling.** E1 emits immutable `TinyEvidence`, not a stable
   handoff. Only an independently verified E2 `HandoffManifest` may grant named consumers
   permission to depend on an interface. Handoffs move through produced, verified,
   accepted, and revoked states; they record the producer head, schema epoch, evidence,
   approver, allowed consumers/environments, retention, readiness ceiling, and revocation.
   Fixtures prove contracts; development goldens prove
   candidate behavior; calibration data may tune only a new version; sealed holdouts prove
   module acceptance; a Staging canary proves bounded operation; natural-refresh soak and
   independent Production evidence are required for graduation. Labels or manual flags
   cannot raise this ceiling.
8. **Do not turn external waits into local idle time.** Pending rights, budget, owner, or
   wall-clock evidence blocks source activation and higher-rung claims, but not fixture/
   local work whose manifest declares that ceiling. Expiry or revocation immediately
   blocks affected live work without erasing historical reproducibility.
9. **Invalidate precisely and preserve failures.** Never edit failed evidence into a pass.
   A local bug reruns the affected rung and all higher rungs. A semantic, PIT, schema,
   threshold, catalog, universe, or selection change creates a new handoff and invalidates
   dependent evidence. Any code fix after candidate freeze is a new candidate/version and
   uses a fresh untouched holdout; a revealed holdout is never rerun as acceptance evidence.
   Denominator shrink is a product-scope change, not a repair.
10. **Close gates only at fan-in.** Capability issues close only at their declared terminal
    rung. Gate epics close in order only when their complete transitive acceptance bundle
    passes on one exact release candidate. The checked-in Vision graph and live GitHub
    parity check must contain every `scope:vision` issue, batch, gate owner, artifact edge,
    terminal rung, and evidence ceiling with no cycle or orphan. Promotion, graduation,
    and rollback remain candidate-wide even though implementation increments merged earlier.

Agents continue across packets in an approved manifest without waiting for a prompt at
each boundary. Escalate only product semantics, security/legal authority, protected-label
custody, a handoff-invalidating scope change, or an external blocker that caps the next
rung. Reports name the batch manifest revision, handoff IDs, rung, evidence, and remaining
readiness ceiling.

## Issue Quality Gate

Every issue must explain a complete causal path from the observed problem to the
project goal. A task list without that argument is not ready for implementation.
Before creating an issue, check `vision.md`, `init.md`, existing issues, and the
current code or evidence so the proposal does not duplicate work or contradict
the authoritative architecture.

An implementation issue is not ready to start unless its exact issue ID is in a versioned
capability-batch manifest, every start dependency is already accepted, and any earlier-rung
input produced inside the batch has passed its declared acceptance. Milestone assignment
alone is insufficient. Provisional fixture work must declare its evidence ceiling; it may
merge after its rung passes but cannot close an issue whose terminal rung is higher.
The issue links its canonical manifest path and SHA-256; copying manifest fields into the
issue body does not make the body authoritative.

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
