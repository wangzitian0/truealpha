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

## Workspace Work Prefix and Deduplication

Derive the workspace identity from the actual repository-root directory for the
current checkout. Never hard-code it or infer it from the remote repository name:

```sh
workspace_root="$(git rev-parse --show-toplevel)"
workspace_name="$(basename "$workspace_root")"
work_prefix="[$workspace_name]"
```

For example, this checkout is rooted at `truealpha`, so its exact work prefix is
`[truealpha]`. A separately named worktree or checkout has its own prefix derived
from its own root directory. Recompute the prefix before creating or renaming work.

1. **Prefix owned work.** Agent task names and every GitHub issue or pull request
   created or exclusively owned by this workspace must use the exact title form
   `<work_prefix> <plain English description>`, for example
   `[truealpha] Freeze the Yahoo parser corpus`. Do not add batch, lane, rung, stage,
   or task tokens such as `[S1]`, `[D1]`, `[S8:E1]`, or `[prep]` to titles. Record
   those identities in the canonical manifest, labels, branch name, or issue/PR body.
2. **Respect PR workspace ownership.** Agents may inspect and work on issues with
   any title prefix. By default, an agent may create, push to, edit, review, resolve
   threads on, or merge only a pull request whose bracketed title prefix exactly
   matches the checkout-derived `<work_prefix>`. Read-only inspection of other PRs
   is allowed when needed for coordination. Acting on a cross-prefix PR requires an
   explicit user instruction that names that PR; a general request to help the
   repository is not an exception. This is an agent operating rule, not a repository
   automation requirement.
3. **Claim one work key.** The unique work key is the issue or batch ID plus its
   target rung, or an explicit standalone task ID when no batch applies. Only one
   active agent, issue, and pull request may own a work key at a time.
4. **Search before creating.** Before opening an issue, branch, or pull request,
   search open and recently closed issues and pull requests, remote branches, and
   canonical batch manifests for the same work key. Continue the existing work
   instead of creating a parallel copy.
5. **Keep parallel work disjoint.** A shared prefix does not make work independent.
   Agents in this workspace may run concurrently only when both their work keys and
   writable paths are disjoint. Changes to shared graphs, migrations, registries,
   exports, lockfiles, or authoritative docs remain serialized through the existing
   integration-lease rules.
6. **Do not claim shared parents.** Root capability and gate issues remain unprefixed
   unless ownership is explicitly assigned to this workspace. Prefixed batch issues
   and pull requests should reference those shared parents without relabeling them as
   exclusively owned work.
7. **Collapse duplicates immediately.** If duplicate work is discovered, keep the
   complete, verified issue or pull request and close the duplicate with a cross-link
   and a concise reason before either line advances further.
8. **Declare PR ownership structurally.** Every PR body contains exactly one
   `Work-Issue: #<number>`, `Work-Key: <key>`, and `Issue-Action: <action>` field.
   Batch PRs use `<batch-id>:<target-rung>` with `managed-by-batch`; standalone PRs
   use `standalone-<issue>` with `complete-on-merge` or `keep-open`. Free-form issue
   mentions and closing keywords are not lifecycle authority.
9. **Run preflight before editing.** Run `make agent-preflight WORK_ISSUE=<number>`
   before claiming work. It validates the checkout-derived prefix, clean worktree,
   upstream state, open issue state, duplicate open PRs, and delivery graph. A clean
   branch whose upstream was deleted may be repaired automatically; dirty worktrees,
   detached heads, and duplicate claims fail closed.

## Mergeability Definition

`mergeable` means **ready and authorized to merge into the target branch now**. It is
not synonymous with GitHub's `mergeable: MERGEABLE`, which only establishes that Git
can combine the branches without an unresolved content conflict. A PR is mergeable
only when every condition below is true for its exact current head SHA:

1. **Authorized scope.** The PR owns one non-duplicated work key and, when applicable,
   exactly one manifest target rung. Required start dependencies and consumed handoffs
   are accepted, and the diff stays within the declared writable paths and integration
   lease.
2. **Compatible base.** The PR has no merge conflict and audits target-branch drift
   from its recorded base before merge. Advancement of `main` alone does not require a
   rebase. Update the branch and rerun affected evidence when that drift intersects the
   declared writable or read-only paths, integration lease, consumed handoffs, generated
   artifacts, frozen inputs, or otherwise invalidates the PR's evidence. Disjoint drift
   neither blocks merge nor rewrites the stable batch activation base, manifest hash, or
   exact-head evidence. CI records the exact base and head that it tested.
3. **Exact-head evidence.** Every required status check has completed successfully on
   that head SHA, including the declared acceptance and negative commands. Pending,
   skipped when required, stale, cancelled, or failing required evidence is not a pass.
4. **Review complete.** The PR is not a draft, required reviewers have reviewed the
   exact material change, requested changes are addressed, and every review thread is
   resolved. Author statements and bot summaries are not substitutes for required
   evidence or authority.
5. **Truthful claim and closure.** The PR description names the evidence ceiling and
   leaves higher claims open. It has no accidental closing keyword. It closes an issue
   only when that issue's terminal evidence and closure dependencies are accepted; an
   intermediate rung PR links the issue without closing it.
6. **Deployable result.** Merging preserves a deployable `main`: additive migrations
   remain backward compatible, provisional code is disabled from accepted release
   bindings and default runtime selection, and rollback remains possible.
7. **Repository enforcement.** The target-branch ruleset reports no remaining block:
   the branch is current, required checks pass, required review resolution passes, and
   the current user has no bypass that is being used to override these conditions.

An open parent capability, incomplete higher rung, pending closure dependency, or
incomplete release gate does not by itself block a lower-rung PR whose manifest permits
that work and whose claim stays below the corresponding ceiling. Conversely, a green
CI badge, label, approval, conflict-free diff, or completed lower rung alone never makes
a PR mergeable. A PR that changes frozen semantics or a frozen candidate must create the
required new version/candidate and satisfy the invalidation rules before it can merge.

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
   terminal rung, objective, claim and readiness ceiling, stable activation base SHA, exact input handoff
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
   rung and names its manifest hash and handoffs. It audits drift from its recorded base,
   updates only when the compatibility rule above requires it, passes rung-specific checks
   on its exact tested base/head pair, advances the target by at most one rung, and may
   merge independently.
   Dependency-topological merge order and the integration lease serialize shared paths.
   A stage merge never promotes an environment
   or claims gate completion. A required branch update does not change the stable
   activation base; rerun affected evidence and record the exact CI base/head instead.
   Stale evidence cannot merge.
   Capability nodes and incoming edges are assembled from independent files under
   `governance/capabilities/`, and batch nodes are assembled from independent files under
   `governance/batches/`; ordinary capability and batch PRs never edit the shared static
   root/Gate graph. `main` remains deployable:
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
   passes on one exact release candidate. The checked-in static root/Gate graph,
   independently writable capability fragments and batch manifests, and live GitHub parity
   check must together contain every `scope:vision` issue, batch, Gate owner, artifact edge,
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
