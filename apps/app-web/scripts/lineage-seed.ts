/**
 * #1241: seeds the rows that `mart.topt_core_meta_info` reads, on a real schema. TEST-ONLY: no deployed
 * path calls it (AGENTS.md rule 6).
 *
 * Two callers use it. `tests/trace-lineage-mart.test.ts` seeds inside a transaction that it rolls back.
 * `scripts/seed-e2e-fixture.ts` seeds the throwaway CI database and commits. One definition keeps both
 * seeds valid when a table of the capture chain changes.
 *
 * The file lives in `scripts/` and not in `tests/` on purpose. `.dockerignore` leaves `tests/` out of the
 * image build, and `next build` type-checks every `scripts/*.ts` file. A script that imports from `tests/`
 * fails that build.
 *
 * The caller runs `set local session_replication_role = replica` first. That turns off the foreign keys
 * and the insert triggers for the transaction, so the seed holds the rows that the view reads and not the
 * whole capture pipeline. The CHECK constraints stay on.
 *
 * This module imports no connection code. The caller owns the client and the transaction.
 */

import { randomBytes } from "node:crypto";

import type { ClientBase } from "pg";

/** The part of a pg client that the seed needs. A `Client` and a `PoolClient` both fit it. */
export type SeedDb = Pick<ClientBase, "query">;

export const hex = (bytes = 32) => randomBytes(bytes).toString("hex");

const FILING = (accession: string, periodEnd: string, filed: string) => ({
  accession,
  document: null,
  filed,
  form: "10-K",
  fp: "FY",
  fy: 2025,
  period_end: periodEnd,
  statement_form: true,
});

export interface Chain {
  observationId: string;
  sourceVintageId: string;
}

const RETRYABLE = ["interrupted", "rate_limited", "server_error", "transport_error"];
const TERMINAL = ["failed", "success", "unavailable", "unchanged"];

/** One observation and every row the lineage view joins from it. */
export async function seedObservation(
  db: SeedDb,
  args: {
    captureRunId: string;
    schedulePolicyId: string;
    semanticType: string;
    knowableAt: string;
    payload: Record<string, unknown>;
  },
): Promise<Chain> {
  const obligationId = `capture-list-obligation:${hex()}`;
  const sourceRequestId = `source-request:${hex()}`;
  const workItemId = `capture-work-item:${hex()}`;
  const sourceVintageId = `source-vintage:${hex()}`;
  const observationId = `normalized-observation:${hex()}`;
  await db.query(
    `insert into raw.capture_obligations
       (obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
        capture_requirement_id, partition_key, content_sha256)
     values ($1, $2, $3, $4, 'listing', 'listing:test:trace', $5, '2026-03-31', $6)`,
    [obligationId, `capture-campaign:${hex()}`, args.captureRunId, `list-version:${hex()}`, `${args.semanticType}:v1`, hex()],
  );
  await db.query(
    `insert into raw.capture_source_requests
       (source_request_id, content_sha256, source_registry_entry_id, source_policy_id,
        request_fingerprint_version, canonical_request_sha256, subject_refs,
        capture_requirement_ids, partition_key, payload)
     values ($1, $2, $3, 'source-policy:trace-test-v1', 'trace-test:v1', $4,
             '[{"kind":"listing","id":"listing:test:trace"}]'::jsonb, $5, '2026-03-31', '{}'::jsonb)`,
    [sourceRequestId, hex(), `source-registry-entry:${hex()}`, hex(), [`${args.semanticType}:v1`]],
  );
  await db.query(
    `insert into raw.capture_work_items
       (work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
        retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256)
     values ($1, $2, $3, $4, 3, $5, $6, $7, $8)`,
    [workItemId, `capture-campaign:${hex()}`, sourceRequestId, args.schedulePolicyId, RETRYABLE, TERMINAL, hex(), hex()],
  );
  await db.query(
    `insert into raw.capture_obligation_work_bindings (binding_id, obligation_id, work_item_id, content_sha256)
     values ($1, $2, $3, $4)`,
    [`capture-obligation-work-binding:${hex()}`, obligationId, workItemId, hex()],
  );
  await db.query(
    `insert into raw.capture_source_vintages
       (source_vintage_id, content_sha256, source_request_id, source_record_id, raw_object_id, raw_fetch_id, payload)
     values ($1, $2, $3, 'trace-test-record', $4, 1, '{}'::jsonb)`,
    [sourceVintageId, hex(), sourceRequestId, `raw-object:${hex()}`],
  );
  await db.query(
    `insert into staging.capture_normalized_observations
       (observation_id, content_sha256, capture_obligation_id, source_vintage_id, semantic_type,
        semantic_version, subject_kind, subject_id, valid_from, knowable_at, parser_version,
        mapping_version, normalized_payload_sha256, is_restatement, confidence, freshness_state,
        payload, recorded_at)
     values ($1, $2, $3, $4, $5, 'v1', 'listing', 'listing:test:trace', $6::timestamptz, $6::timestamptz,
             'trace-test-parser:v1', 'trace-test-mapping:v1', $7, false, 0.85, 'fresh', '{}'::jsonb,
             $6::timestamptz + interval '1 day')`,
    [observationId, hex(), obligationId, sourceVintageId, args.semanticType, args.knowableAt, hex()],
  );
  await db.query(
    `insert into staging.capture_observation_obligations (capture_obligation_id, observation_id) values ($1, $2)`,
    [obligationId, observationId],
  );
  await db.query(
    `insert into staging.capture_observation_payloads (observation_id, normalized_payload_sha256, normalized_payload)
     values ($1, $2, $3::jsonb)`,
    [observationId, hex(), JSON.stringify(args.payload)],
  );
  return { observationId, sourceVintageId };
}

export interface ListingSeed {
  listingId: string;
  instrumentId: string;
  shares: string;
  close: string;
}

export interface SeededListing {
  financial: Chain;
  price: Chain;
}

/** One issuer's core result in one capture run, with the four observations of each of its listings. */
export async function seedResult(
  db: SeedDb,
  args: {
    captureRunId: string;
    entityId: string;
    cutoff: string;
    resultHashPrefix: string;
    createdAt: string;
    grossProfit: string;
    listings: ListingSeed[];
  },
): Promise<{ resultId: string; byListing: Map<string, SeededListing> }> {
  const schedulePolicyId = `schedule-policy:${hex()}`;
  await db.query(
    `insert into raw.capture_schedule_policies
       (schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence,
        freshness_max_age, retry_policy, payload)
     values ($1, $2, 'trace-test:v1', interval '1 day', 'manual-only:v1', interval '30 days',
             '{}'::jsonb, '{}'::jsonb)`,
    [schedulePolicyId, hex()],
  );
  const byListing = new Map<string, SeededListing>();
  const observationIds: string[] = [];
  for (const listing of args.listings) {
    const identity = { issuer_id: args.entityId, instrument_id: listing.instrumentId, listing_id: listing.listingId };
    const common = { captureRunId: args.captureRunId, schedulePolicyId };
    const financial = await seedObservation(db, {
      ...common,
      semanticType: "financial-fact",
      knowableAt: "2026-02-03T00:00:00Z",
      payload: {
        ...identity,
        operating_branch: "non_financial",
        currency: "USD",
        gross_profit: args.grossProfit,
        total_assets: "200000000",
        headcount: "100",
        revenue: "100000000",
        shares_outstanding: listing.shares,
        pre_provision_profit: null,
        vintage: {
          gross_profit: FILING("0000320193-26-000010", "2025-12-31", "2026-02-01"),
          total_assets: FILING("0000320193-26-000010", "2025-12-31", "2026-02-01"),
          revenue: FILING("0000320193-26-000011", "2025-09-30", "2026-01-15"),
          shares_outstanding: FILING("0000320193-26-000012", "2025-10-17", "2026-02-03"),
          // The headcount names its own pointer and time and no filing.
          headcount: {
            accession: null,
            evidence_ref: "annual report p. 4",
            filed: null,
            knowable_at: "2026-01-20T00:00:00+00:00",
            period_end: "2025-06-30",
            raw: "raw.fetches:7",
          },
        },
      },
    });
    const price = await seedObservation(db, {
      ...common,
      semanticType: "market-price",
      knowableAt: "2026-03-30T21:00:00Z",
      payload: { ...identity, currency: "USD", close: listing.close },
    });
    const listed = await seedObservation(db, {
      ...common,
      semanticType: "listing-identity",
      knowableAt: "2026-01-01T00:00:00Z",
      payload: { ...identity, ticker: "TRACE" },
    });
    const member = await seedObservation(db, {
      ...common,
      semanticType: "universe-membership",
      knowableAt: "2026-01-01T00:00:00Z",
      payload: { ...identity, ticker: "TRACE" },
    });
    observationIds.push(financial.observationId, price.observationId, listed.observationId, member.observationId);
    byListing.set(listing.listingId, { financial, price });
  }
  observationIds.sort();
  // The result names the first listing by instrument id, as `_issuer_factor_input` does.
  const named = [...args.listings].sort((a, b) => (a.instrumentId < b.instrumentId ? -1 : 1))[0];
  const resultSha = args.resultHashPrefix + hex().slice(args.resultHashPrefix.length);
  const resultId = `topt-core-result:${resultSha}`;
  await db.query(
    `insert into mart.topt_core_results
       (result_id, content_sha256, invocation_id, snapshot_id, run_id, release_manifest_id, universe_id,
        universe_version, universe_sha256, cutoff, issuer_id, instrument_id, listing_id, operating_branch,
        operating_metric, availability, operating_efficiency, capital_adjusted_gross_profit, gppe, tier,
        target_ps_lower, target_ps_upper, target_ps_midpoint, current_ps, valuation_gap, confidence,
        freshness, reason_codes, input_observation_ids, gppe_invocation_id, gppe_result_id,
        gppe_definition_id, gppe_definition_sha256, tier_definition_id, tier_definition_sha256, payload,
        created_at, availability_status, source_evidence_status, factor_validation_status)
     values ($1, $2, $3, $4, $5, $6, 'universe:topt-test', 'v1', $7, $8::timestamptz, $9, $10, $11,
             'non_financial', 'capital_adjusted_gppe', 'available', 12.5, 1, 12.5, 'tech',
             15, 20, 18.75, 20, 0.5, 0.85, 'fresh', '{}', $12, $13, $14, $15, $16, $17, $18, '{}'::jsonb,
             $19::timestamptz, 'available', 'verified', 'not_evaluated')`,
    [
      resultId,
      resultSha,
      `topt-core-invocation:${hex()}`,
      `topt-core-snapshot:${hex()}`,
      args.captureRunId,
      `release-manifest:${hex()}`,
      hex(),
      args.cutoff,
      args.entityId,
      named.instrumentId,
      named.listingId,
      observationIds,
      `topt-gppe-invocation:${hex()}`,
      `topt-gppe-result:${hex()}`,
      `gppe-definition:${hex()}`,
      hex(),
      `three-tier-definition:${hex()}`,
      hex(),
      args.createdAt,
    ],
  );
  return { resultId, byListing };
}
