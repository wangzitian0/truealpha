# A7 — The standard→wide-row loop: MetricStandard, missing-cell planner, and gated extraction

Status: Accepted. Codifies the architecture delivered across #735, #70, #732, #733, #734,
#528, #729, PR #740, #754, #765, #799, #800, #908, #909, #917, and #1038.
Date: 2026-09-04 (amended 2026-10-09)

## Context

Fundamental research was starved by missing denominators and unextracted disclosures
(#735): 89 of 101 QQQ issuers lacked gross profit per employee (GPPE) because headcount was
unreported in standard XBRL; TOPT 20 relied on unverified manual seeds without accession
pointers; and `libs/factors/shared/extraction.py` was an empty stub.

Past attempts suffered from six recurrent defect shapes ("the fakes"):
1. **The Headcount Special-Case Fake**: The initial loop accepted `standard=` in signatures
   but hardcoded `staging.issuer_headcount_facts` and `extract_headcount` inside the planner
   and backfill functions. It proved the parameter, not the wiring (#799).
2. **The Metric-Default Trap**: The backfill configuration defaulted to `"employees_total"`,
   so a weekly schedule passing no argument ran only one standard while `segment_revenue`
   sat orphaned (#806).
3. **Ingestion Clock Masquerading as Knowable-at**: Values stamped with `datetime.now()`
   created lookahead bias in historical backtests.
4. **Non-deterministic Replay / Re-asking Models**: Model calls with renamed aliases or
   re-runs that asked providers again burned tokens and caused flapping judgements.
5. **Values without Verbatim Evidence**: Extractions lacking physical byte provenance
   could not be audited or reproduced.
6. **Stale Governance Blockers**: Assuming non-production work remained blocked on owner
   approvals contradicted the autonomous stage checklist principle (infra2#1035).

This ADR establishes the unified, falsifiable Single Source of Truth (SSOT) for the loop.

## Decision 1 — MetricStandard is the single source of truth for metric meaning

A metric's meaning, acceptance criteria, evidence rules, fact plane, and extraction
adapter are declared once in `truealpha_contracts.standards.MetricStandard`.

- **Zero-migration expansion**: A new standard registers in `STANDARDS` as code. It requires
  zero SQL migrations.
- **Explicit FactPlane**: Each standard owns its table, issuer column, winning source
  priority, and withdrawn extractors tuple.
- **Two kinds, one row**: `HARD` standards require deterministic rules; `EVALUATIVE`
  standards carry `factor_validation_status` and cannot enter strategy eligibility while
  `unvalidated` (Milestone M2, #773).

## Decision 2 — Missing-cell planning derives from declared demand

`datahub.standards.planner.open_cells` left-joins declared universe demand against existing
observations:
- A cell is open only when: (1) no fact is knowable at the cutoff (`no_fact`), (2) the best
  fact is an uncorroborated seed (`seed_only`), or (3) the best fact exceeds `max_age_days`
  (`stale`).
- Winning observations follow declared `source_priority` before recency (init.md rule 12).
- Closed cells are never re-fetched: vendor spend is proportional to actual missing cells.

## Decision 3 — All external calls are gated and recorded in api_call_ledger

All external vendor and model requests pass through `data_engine.sources.gateway.SourceGateway`:
- Windowed rate limits and daily budgets are enforced per seat (SEC, Twelve Data, Yahoo,
  OpenFIGI, N-PORT, and `filing-extraction-model`). Over-capacity requests are deferred.
- Every external call produces one row in `staging.api_call_ledger`.
- Model invocations are appended to `staging.model_invocations`. Replay matches both
  requested model digest and `served_model`; `order by id asc limit 1` guarantees that the
  first answer is immutable.

## Decision 4 — Strict physical evidence and point-in-time invariants

- **Verbatim span requirement**: Landed observations must link to captured document bytes in
  `raw.fetches` with exact accession, filing form, and sentence quotation.
- **Filing date is knowable-at**: `knowable_at` is derived from the filing date, never the
  fetch clock.
- **Predecessor CIK fallback**: Post-reorganization holding companies with no filings under
  their new CIK resolve through `staging.issuer_cik_predecessors`.

## Standing Guards (Drift & Regression Prevention)

The following standing checks protect this architecture in CI:

1. `tools/check_factor_contract.py`:
   Asserts that public factor signatures and datahub tables match the frozen contract.
2. `apps/data-engine/tests/test_standard_loop_is_generic.py`:
   Drives the loop with a synthetic standard `OTHER` on an isolated plane, asserting that the
   planner and backfill query only what the standard declares without schema migration.
3. `apps/data-engine/tests/test_standards_lane_schedule.py`:
   Asserts that the weekly Dagster schedule covers every registered standard in `STANDARDS`.
4. `apps/data-engine/tests/test_source_gateway.py`:
   Asserts that a capacity of 1 defers the second call of the day and proves rate limiting.
5. `apps/data-engine/tests/test_standard_backfill.py`:
   Asserts that replayed model answers query the provider zero times, and holding company
   predecessors resolve correctly.

## Consequences

- The standard→wide-row loop is fully generalized across hard and evaluative standards.
- Production backfill runs weekly without manual intervention or redundant model costs.
- Adding a new metric standard requires only adding a `MetricStandard` entry and its adapter.
