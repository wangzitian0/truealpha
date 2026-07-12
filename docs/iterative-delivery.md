# Iterative Delivery Model

TrueAlpha is developed as a data application, not as a sequence of fully specified
platform layers. Work advances through a repeatable evidence ladder:

| Rung | Purpose | Maximum claim |
|---|---|---|
| E0 Code | Smallest typed vertical slice and negative contract tests | Experimental code |
| E1 Tiny | End-to-end run on a predeclared edge-case corpus | Contract behavior |
| E2 Contract repair | Fix toolkit/schema/interaction gaps and publish a pinned handoff | Stable local handoff |
| E3 Medium | Stratified multi-subject, multi-cutoff, multi-vintage run | Development candidate |
| E4 Harden/freeze | Performance, failure injection, recovery, freeze, blind holdout, bounded Staging canary | Accepted module/candidate or bounded operation |
| E5 Large/shadow | Empty full build, capacity, recovery, real consumers, observed natural refresh | Large-scale shadow evidence only |

Release gates remain ordered acceptance boundaries. They do not prevent a downstream
agent from starting fixture/local work after its exact E2 handoff exists. Rights, budget,
holdout custody, and natural-refresh waits cap the readiness claim instead of making local
workers idle.

Gate acceptance and Production graduation are not an E6 capability rung. They are a
machine-checked fan-in over one exact candidate: all terminal issue evidence, independent
capture audit, final Vision audit, and human approval must agree. E5 never closes a gate by
itself.

## Lanes And Handoffs

- **Capture/storage** owns source calls, raw/normalized persistence, row-complete manifests,
  source policy, and PIT snapshot inputs.
- **Orchestration/platform** owns Dagster composition, runtime resources, retries, and
  environment execution.
- **Strategy** owns factors, screens, rules, replay, and metrics under `libs/factors`.
- **Consumption** owns mart reads, MCP, reports/cards, App, and chat over materialized DTOs.
- **Verification/operations** owns independent oracles, SLO evaluation, recovery, promotion,
  natural-refresh evidence, and final audits.

Contracts/toolkit is a shared integration surface, not a sixth concurrent lane. A single
lease owns typed models, public exports, compatibility, conformance, schema epochs,
registries, migration numbering, generated artifacts, root lockfiles, and authoritative
architecture documents.

Capture hands strategy only a ready `CaptureEvaluationReport`, exact `SnapshotManifest`,
and typed factor inputs. Strategy hands consumers only materialized outputs and their
catalog, universe, release, availability, trace, usage, and quality-review identities.
Every consumer pins an exact handoff ID and hash; `latest` is never a valid dependency.

## Dependency Classes

- A **start dependency** blocks even provisional implementation because the consumer
  cannot form a valid typed interaction without it.
- A **freeze dependency** allows fixture/local work, but blocks publishing a stable
  candidate or handing it to a protected evaluator.
- A **closure dependency** allows implementation and candidate freeze, but blocks the
  capability issue or release gate from closing until independent or operational evidence
  exists.

Whole milestones are never start dependencies. An issue reference without an exact
artifact type and acceptance condition is informational rather than a hard edge.

## Current Launch

The first wave contains two E1 discovery batches. They remain queued until this governance
change is merged, #81 enforces activation against the actual PR diff, and their manifests
are regenerated against the current `main` base SHA.
Listing a future packet is not implementation authorization. Packets may run concurrently
only where writable paths are disjoint and start dependencies are satisfied.

| Packet | Issues | Batch status | First target | Output / ceiling |
|---|---|---|---|---|
| S0 core strategy | #21, #24, #25 | #78 queued | E0-E1 | `CoreTinyEvidence`; provisional fixture behavior only |
| D0 MVP data path | #22, #14, #23, #70 | #79 queued | E0-E1 | `MvpCaptureTinyEvidence`; provisional capture/PIT behavior only |
| V0 research semantics | #59 | next manifest | E0-E2 | Local-stable semantics/Catalog/oracle protocol; no Gate freeze or protected labels |
| R0 readiness | #60, #61 | next manifest | E0-E2 | Source/rights reconnaissance and applicability/SLO evaluators; final hashes wait for V0 |
| C0 read contract | #41 | next manifest | E0-E1 | Python/TypeScript fixture read/trace/usage DTO conformance; no all-module SQL claim |

S0 may prototype against checked-in candidate defaults, but all observed numerical,
ranking, and return outputs are calibration and cannot support #59 approval or independent
evidence. The semantic approver does not receive those outcomes before the #59 hash freezes.
D0 may use authorized public fixtures while Production rights/budget decisions remain
pending. C0 may define fixture read contracts before all-module replay, but final mart SQL
conformance waits for #40.

Neither discovery batch closes its capability issues or publishes a stable handoff. If a
generic defect appears, a separate E2 contract-repair batch versions the shared surface and
reruns the exact E1 corpus. Before #26 closes or any integrated Staging/Production claim, an
integration batch must consume the accepted E2 capture handoff and the exact S0 candidate,
then prove captured headcount -> snapshot -> GPPE -> tier/decision with trace, usage, and
reverse review. #71 may validate the frozen provenance-blind factor candidate first; #26
then proves the accepted capture implementation feeds that same candidate.

## Hard Dependencies

Keep dependencies on exact artifacts, not whole milestones. The critical examples are:

- #59 -> final #60 source matrix; #59 + #60 -> final #61 applicability/SLO bundle.
- #22 -> #14 independent composite-evidence audit and #23 runtime data path.
- #23 -> #70 is a stable-input freeze edge for #24; #24 -> #25; frozen #24/#25 ->
  independent #71 holdout. #78 can test provisional factor behavior but cannot satisfy
  these freeze or closure edges.
- #62 -> #34/#35, #63 -> #36, #64 -> #37/#39, and #37 -> #38.
- Frozen module candidates -> #65 holdout -> #40 shared replay.
- #41 -> #42/#43/#45; #43 -> #44; #42 -> #46; all consumer paths -> #48.
- Exact Staging/recovery/consumer evidence -> #67 candidate -> #68 audit -> #54 graduation.

Do not use milestone closure as an implementation dependency. In particular, generic #22
work does not require #21; #24 uses #70 rather than Gate 2's #64; #41 may start contract-
first before #40; recovery tooling may be built before its exact-candidate drill; and
independent verifier issues consume frozen candidate hashes rather than blocking candidate
implementation.

## Batch And Handoff Manifests

The canonical batch manifests live under `governance/batches/`; GitHub issue bodies link
their path and SHA-256 but are not authoritative. Each manifest records version/revision,
issue IDs, last-accepted/current-target/terminal rung, objective, claim/readiness ceiling,
base SHA, exact
dependency handoffs and classes, environment/corpus/denominator, PIT rules, source approval,
paths and integration lease, commands/negative controls/budgets, expected artifacts and
retention, owners/reviewer/custodian, invalidation, merge predecessors, and rollback.

One batch may declare a contiguous multi-rung range, but one PR accepts exactly its current
target and moves the target by at most one rung. A new batch starts with no accepted rung
and target E0. A rebase changes the manifest revision/hash;
all affected evidence reruns before merge. Failed evidence is retained. A semantic, PIT,
schema, threshold, catalog, universe, or selection change creates a new handoff and
invalidates dependent evidence. Denominator shrink is an explicit product-scope revision,
never a repair for a failed run.

E1 emits `TinyEvidence`. An E2 `HandoffManifest` is the first stable dependency and has a
canonical serialization plus lifecycle state: produced, independently verified, accepted,
or revoked. It binds producer head, schema epoch, evidence hashes, approver, allowed
consumers/environments, retention, readiness ceiling, and revocation. CI rejects a consumer
whose exact accepted handoff is missing, altered, revoked, or outside its permission set.

The checked-in Vision graph assigns every `scope:vision` issue to exactly one gate, links
each batch to its owner gate, and records typed artifact edges. Offline validation proves
uniqueness and acyclicity; CI compares it with live GitHub labels, milestones, batch status,
and manifest hashes. A gate cannot close while graph parity fails.

The current checker intentionally does not authorize #78/#79: #81 must add merge-base,
corpus-byte, changed-path/glob, shared-lease, command-evidence, and accepted-handoff checks
before either queued manifest may become active.
