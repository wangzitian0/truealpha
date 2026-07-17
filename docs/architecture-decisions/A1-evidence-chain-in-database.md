# A1 — Evidence chain lives in the database, behind a storage-neutral port

Status: Draft (needs owner sign-off — amends the `AGENTS.md` consumer-read red line, referenced from `init.md`)
Date: 2026-07-18
Supersedes the operative parts of: file-based content-hash pinning as an enforcement/consumption mechanism.

## Context

Point-in-time (PIT) correctness is TrueAlpha's core invariant: every output must be
traceable to what was knowable at a historical moment. Today that invariant is protected
by a sprawl of content-addressed SHA-256 identities — checked-in handoff/manifest files,
snapshot/invocation hashes, and a downstream read API that forces consumers to hand-carry
a 7-part identity tuple (`run_id + release_manifest_id + universe_id/version/sha256 +
snapshot_id + invocation_id`) with no `latest` and no discovery.

Observed problems:

- The hash sprawl blocks development velocity and does not scale to a large database; the
  automated governance/hash enforcement machine was already removed (2026-07-17).
- Downstream cannot read anything without an exact hash tuple — no governed "current"
  pointer, no discovery. Staging today has real captured data in Postgres but an **empty
  `mart`** and no consumer path.
- The evidence chain is fundamentally a **relational + temporal graph** (raw → normalized
  → snapshot → invocation → result, with restatement supersession), which a database
  enforces far better than file fingerprints.

## Decision

1. **The evidence chain is managed in the database, transactionally.** Provenance,
   lineage, PIT validity, immutability, and completeness are enforced by database
   transactions, constraints, bitemporal columns, and append-only rules — not by file
   hashes and not by application-side ceremony.

2. **Storage is abstracted behind a port.** The evidence chain is modeled as a
   backend-neutral **provenance graph** (nodes + typed edges + bitemporal stamps) exposed
   through a repository port in `libs/contracts`. Short-term backend: **Postgres (RDS)**.
   Future backend: a **graph database**, added as a second adapter with no change to
   factors or consumers.

3. **Hashes shrink to two load-bearing columns**, never a consumer-facing API:
   - `payload_sha256` on immutable **raw blobs** (S3 object integrity the DB cannot see).
   - one **release-attestation hash** on the release manifest row (cross-system deploy
     trust).
   All other identities (snapshot, invocation, handoff) become **surrogate keys + foreign
   keys + transaction boundaries**.

4. **Downstream reads resolve through a governed current-pointer, not a hash tuple.**
   Consumers ask for "the current TOPT core for universe X"; the database resolves the
   exact node and its provenance closure inside a transaction, under `mart_readonly` +
   publication-policy authorization. This **amends the `init.md` red line**
   "Consumers … pin exact snapshot and handoff identities, never `latest`" to: consumers
   resolve a governed, access-controlled head pointer; exact historical identities remain
   queryable and immutable, but are no longer hand-carried.

Reproducibility is not lost: replay reads the exact immutable historical rows by PIT query
(valid_time / transaction_time), which is stronger than a fingerprint because it is the
data itself, and it is queryable.

## The storage-neutral evidence model (the heart of "通用")

Modeled once in `libs/contracts`, independent of any backend:

- **Evidence nodes** (kinds): `raw_fetch`, `source_vintage`, `normalized_observation`,
  `snapshot`, `factor_invocation`, `materialized_result`, `capture_run`, `obligation`,
  `quality_cell`, `release_manifest`.
- **Provenance edges** (relations): `derived_from`, `selected_from`, `member_of`,
  `bound_to`, `attested_by`, `supersedes` (restatement).
- **Bitemporal stamp** on every node/edge: `valid_time` (or period) + `transaction_time`
  (from a source property, never an insertion clock) + `recorded_at` (audit only).
- **Append-only**: no in-place update; restatements insert new nodes/edges with
  `supersedes`. Parsed facts carry `mapping_version`.
- **Current pointer**: a governed `head` reference per `(environment, release, universe,
  factor)` → the exact current node, access-controlled.
- **Provenance closure**: a bounded traversal from any node to its full lineage (and
  reverse), returned as typed DTOs.

Ports (backend-neutral):

- `EvidenceGraphWriter` — `put_node`, `put_edge` inside a **unit-of-work** (one
  transaction per pipeline run); append-only enforced.
- `EvidenceGraphReader` / `ResearchReadRepository` — resolve current pointer, fetch node +
  bounded provenance closure, typed projections; no raw bytes by default.
- `CurrentPointerRegistry` — governed head refs; publication-policy checked before read.

Factors stay provenance-neutral (unchanged red line): they see typed records with opaque
input identity, never the backend, edges, or hashes.

## Adapters

**Postgres / RDS (short-term, build now):**
- Typed node tables + one indexed `lineage_edges` table (`from_kind, from_id, to_kind,
  to_id, relation`, backed by indexed relational keys — no full-JSON-scan trace).
- Bitemporal columns; append-only via existing `reject_mutation`-style triggers.
- `unit-of-work` = one Postgres transaction wrapping capture → normalize → snapshot →
  materialize (all-or-nothing).
- `current_pointer` table behind `mart_readonly` + `app.authorization_decisions`.
- PIT reads via bitemporal predicates; trace via indexed edge joins.

**Graph database (future, documented seam only):**
- Nodes → vertices, `lineage_edges` → native edges, closure → native traversal.
- Same ports, same DTOs → **zero change to factors and consumers**.
- Migration is an ETL of the append-only node/edge set; hashes are not required to
  reconstruct identity because surrogate keys + edges carry it.

## Hash policy (cross-cutting)

- **Keep**: `raw.fetches.payload_sha256`; one release-attestation hash.
- **Drop**: consumer-facing hash tuples; file-based handoff/manifest gating; `never latest`
  as an absolute (replaced by governed pointer).
- Tests pin surrogate identities + PIT queries, not content SHAs, except for the raw-blob
  integrity check.

## Implementation plan (phased, mapped to issues)

**Phase 0 — Sign-off & ADR (this doc).** Owner approves the red-line amendment. Foundational
to Gate 0 semantic/data closure (#56/#57–#61). No code depends on unfrozen interfaces first.

**Phase 1 — Backend-neutral evidence model (`libs/contracts`).** Define node/edge/bitemporal
DTOs, the three ports, `ProvenanceClosure`, and `CurrentPointer`. Pure contracts + fixture
conformance; no backend. This is the reusable core. (Feeds #58 executable lineage
contracts.)

**Phase 2 — Postgres adapter + migrations.** Add `lineage_edges`, `current_pointer`,
bitemporal columns, append-only triggers, and the unit-of-work. Retire the hash-tuple read
path. Bring `db/migrations` forward; keep migrations backward-compatible. (Supports #41.)

**Phase 3 — Capture spine on the model (#171).** The executor/dispatcher iterates the 84
`CaptureWorkItem`s, calls the four real semantic adapters (`market-price` Decimal-safe,
`financial-fact`, `listing-identity`, `universe-membership`), lands raw+checksum, writes
nodes/edges in one transaction, and produces the row-complete quality report as a DB
projection. "Ready" only at 84/84.

**Phase 4 — Downstream supply (#41 / #362).** Governed current-pointer + typed `mart` views
via `mart_readonly` + publication policy. App `/admin/strategy-runs` and MCP `strategy_run`
read real `mart` data; retire `FixtureStrategyRunRepository`; fix the README "App reads mart
directly" claim.

**Phase 5 — Bring staging forward.** Migrate `truealpha-postgres-staging` from the current
D2-generation schema to the new model (it is missing 0022–0026 and has an empty `mart`).
Run the governed capture on the existing host ingestion runtime into staging, materialize to
`mart`, and verify App/MCP reads against staging. (This is the concrete "make staging
runnable" milestone.)

**Phase 6 — Graph-DB readiness (future, not built).** Keep the port seam documented; add the
graph adapter when needed. No consumer/factor change required.

## Refinements (owner decisions, 2026-07-18)

1. **Every run carries a content hash; downstream refreshes when the pointer advances.**
   Each capture/materialization run has a per-run content hash as its stable identity. The
   `current_pointer` advances to the newest run's hash on refresh. Downstream reads through
   the pointer; a consumer that cached an older run-hash detects the pointer moved and
   re-pulls. So: pointer for discovery + per-run hash for identity/reproducibility +
   mandatory downstream refresh on advance.
2. **Hash is demoted to an integrity column** (confirms Decision 3). The per-run/per-blob
   hash is stored as a column for integrity/dedup/reproducibility; the **reference and
   consumption path uses surrogate keys + FK edges + the pointer**, never a hand-carried
   hash tuple. #58 stays closed (additive change, no evidence invalidation).
3. **Capture completeness is error-code governance, not a binary pass/fail.** Every
   obligation reaches a **terminal state with a classified reason code**, and each code
   carries a **disposition**:
   - **STOP (fatal, halt the run)**: auth failure, contract/schema violation,
     release/scope mismatch, look-ahead violation (`knowable_at` after cutoff), raw-blob
     checksum mismatch. The run is invalid.
   - **RETRY (bounded)**: transient network/timeout, HTTP 429/5xx; after N attempts,
     escalate to STOP or resolve as `unavailable`.
   - **TRACE-ONLY (record and continue)**: not-yet-knowable/pending, field genuinely
     absent for this issuer (`unavailable`), low-confidence source. Recorded as a terminal
     cell with a reason code; the run continues.
   All states and transitions are traceable through the provenance graph and surfaced on
   the #61 dashboard. A run "succeeds" when all 84 obligations are terminally resolved with
   no STOP outstanding — not when all 84 are `available`.
4. **Scope of the first long run: Phases 1–5** (contracts → Postgres adapter → capture
   spine → downstream reads → staging brought up and verified). Gate 4 graduation and the
   graph backend are out of scope.

## Non-goals

Production graduation (Gate 4 / #67/#68/#54), the containerized data-engine image and infra2
deploy receiver (owned by the infra2 repo owner; blocked on infra2 #500), confidence-formula
calibration (#207), and building the graph backend now.

## Risks

- SEC XBRL tag/unit variance across issuers makes the `financial-fact` adapter the main
  correctness risk in Phase 3.
- The red-line amendment must be signed off before Phases 2–4 rely on it.
- Staging schema is a generation behind; Phase 5 must migrate forward without rewriting
  existing append-only history.
