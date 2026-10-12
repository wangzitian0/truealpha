/**
 * #1241: the trace page reads the lineage of a decision, through the server function the deployed
 * page calls (`loadTrace`), against a real Postgres. It skips without one and fails when armed
 * (ci-web provides the database and TRUEALPHA_REQUIRE_RUNTIME=1).
 *
 * The seed is one strategy run, one decision, one capture run and the full chain that
 * `mart.topt_core_meta_info` joins: four observations (financial fact, price, identity,
 * membership) with their obligations, work items, source requests, source vintages and payloads.
 * A second capture run holds a decoy core result for the same issuer and cutoff, newer and with
 * other values. It is not bound to the strategy run. The page must show the bound run, never the
 * newest row.
 *
 * Everything runs in ONE transaction on the test's own client, lent to the read layer through
 * db.ts's `__setTestClient` seam, and is rolled back. `session_replication_role = replica` turns
 * off the foreign keys and the insert triggers of the capture tables for this transaction, so the
 * seed holds the rows the view reads and not the whole capture pipeline. CHECK constraints stay on.
 * Run standalone: `ALLOW_CLEAR_TEST=1 DATABASE_URL=... bun run tests/trace-lineage-mart.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { Client, type PoolClient } from "pg";

import type { AccessContext } from "../src/contracts/strategyRun";
import { loadTrace } from "../src/server/dashboard";
import { __setTestClient } from "../src/server/mart/db";
import { NOT_RECORDED, type TraceInput } from "../src/server/mart/research-read";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const dbUrl = requireDb("trace-lineage-mart", { write: true });
if (!dbUrl) {
  process.exit(0);
}
const DATABASE_URL: string = dbUrl;
process.env.DATABASE_URL = DATABASE_URL;
const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);

const CONTEXT: AccessContext = {
  contextId: "ctx:trace-lineage-mart",
  principalId: "principal:test",
  tenantId: "tenant:test",
  sessionId: "session:test",
  authenticationMethod: "service_identity",
  issuedAt: "2026-10-12T00:00:00Z",
  expiresAt: "2026-10-12T01:00:00Z",
};

const hex = (bytes = 32) => randomBytes(bytes).toString("hex");
const CUTOFF = "2026-03-31T00:00:00Z";

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("trace-lineage-mart: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

const FILING = (accession: string, periodEnd: string) => ({
  accession,
  document: null,
  filed: "2026-02-01",
  form: "10-K",
  fp: "FY",
  fy: 2025,
  period_end: periodEnd,
  statement_form: true,
});

interface Chain {
  observationId: string;
  sourceVintageId: string;
}

const RETRYABLE = ["interrupted", "rate_limited", "server_error", "transport_error"];
const TERMINAL = ["failed", "success", "unavailable", "unchanged"];

/** One observation and every row the lineage view joins from it. */
async function seedObservation(
  db: Client,
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

/** A capture run: one policy, four observations and the core result that lists them. */
async function seedCaptureRun(
  db: Client,
  args: {
    captureRunId: string;
    entityId: string;
    resultHashPrefix: string;
    createdAt: string;
    financial: Record<string, unknown>;
    close: string;
    financialKnowableAt: string;
    priceKnowableAt: string;
  },
): Promise<{ resultId: string; financial: Chain; price: Chain }> {
  const schedulePolicyId = `schedule-policy:${hex()}`;
  await db.query(
    `insert into raw.capture_schedule_policies
       (schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence,
        freshness_max_age, retry_policy, payload)
     values ($1, $2, 'trace-test:v1', interval '1 day', 'manual-only:v1', interval '30 days',
             '{}'::jsonb, '{}'::jsonb)`,
    [schedulePolicyId, hex()],
  );
  const identity = { issuer_id: args.entityId, instrument_id: "instrument:test:trace", listing_id: "listing:test:trace" };
  const common = { captureRunId: args.captureRunId, schedulePolicyId };
  const financial = await seedObservation(db, {
    ...common,
    semanticType: "financial-fact",
    knowableAt: args.financialKnowableAt,
    payload: {
      ...identity,
      operating_branch: "non_financial",
      currency: "USD",
      ...args.financial,
      pre_provision_profit: null,
    },
  });
  const price = await seedObservation(db, {
    ...common,
    semanticType: "market-price",
    knowableAt: args.priceKnowableAt,
    payload: { ...identity, currency: "USD", close: args.close },
  });
  const listing = await seedObservation(db, {
    ...common,
    semanticType: "listing-identity",
    knowableAt: "2026-01-01T00:00:00Z",
    payload: { ...identity, ticker: "TRACE" },
  });
  const membership = await seedObservation(db, {
    ...common,
    semanticType: "universe-membership",
    knowableAt: "2026-01-01T00:00:00Z",
    payload: { ...identity, ticker: "TRACE" },
  });
  const ids = [financial, price, listing, membership].map((chain) => chain.observationId).sort();
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
      CUTOFF,
      args.entityId,
      identity.instrument_id,
      identity.listing_id,
      ids,
      `topt-gppe-invocation:${hex()}`,
      `topt-gppe-result:${hex()}`,
      `gppe-definition:${hex()}`,
      hex(),
      `three-tier-definition:${hex()}`,
      hex(),
      args.createdAt,
    ],
  );
  return { resultId, financial, price };
}

function input(inputs: readonly TraceInput[], key: string): TraceInput {
  const found = inputs.find((candidate) => candidate.key === key);
  assert(found !== undefined, `no input ${key}`);
  return found;
}

function instant(text: string | null): string | null {
  return text === null ? null : new Date(text).toISOString();
}

const admin = await reachable();
if (admin !== null) {
  try {
    await admin.query("begin");
    await admin.query("set local session_replication_role = replica");
    __setTestClient(admin as unknown as Pick<PoolClient, "query">);

    const suffix = hex(6);
    const entityId = `11111111-1111-5111-8111-${suffix}`;
    const legacyId = `issuer:lei:TRACE${suffix.toUpperCase()}`;
    const strategyRunId = `strategy-run:${hex()}`;
    const boundCaptureRun = `capture-run:${hex()}`;
    const decoyCaptureRun = `capture-run:${hex()}`;

    // The page shows the legacy id. The mart rows hold the entity id. The trace read must translate.
    await admin.query(
      `insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value)
       values ($1, 'issuer', 'uuidv5:v1', 'trace-test', 'lei', $2)`,
      [entityId, legacyId],
    );
    await admin.query(
      `insert into staging.entity_aliases
         (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
       values ($1, 'legacy-id', $2, '2026-01-01', '2026-01-01T00:00:00Z', 'trace-test', 'trace-test', 'asserted', 1, 'trace-test:v1')`,
      [entityId, legacyId],
    );

    const bound = await seedCaptureRun(admin, {
      captureRunId: boundCaptureRun,
      entityId,
      resultHashPrefix: "ff",
      createdAt: "2026-04-01T00:00:00Z",
      financial: {
        gross_profit: "210000000",
        total_assets: "200000000",
        headcount: "100",
        revenue: "100000000",
        shares_outstanding: "10000000",
        vintage: {
          gross_profit: FILING("0000320193-26-000010", "2025-12-31"),
          total_assets: FILING("0000320193-26-000010", "2025-12-31"),
          revenue: FILING("0000320193-26-000011", "2025-09-30"),
          shares_outstanding: FILING("0000320193-26-000012", "2025-10-17"),
          // The headcount names evidence and no filing: its accession is null.
          headcount: { accession: null, evidence_ref: "seed", period_end: "2025-06-30", raw: null },
        },
      },
      close: "40",
      financialKnowableAt: "2026-02-01T00:00:00Z",
      priceKnowableAt: "2026-03-30T21:00:00Z",
    });
    // The decoy: same issuer and cutoff, a newer core result, other values, a capture run that no
    // strategy run is bound to. Its result id sorts first, so a read that ignores the binding finds it.
    await seedCaptureRun(admin, {
      captureRunId: decoyCaptureRun,
      entityId,
      resultHashPrefix: "00",
      createdAt: "2026-05-01T00:00:00Z",
      financial: {
        gross_profit: "999",
        total_assets: "999",
        headcount: "999",
        revenue: "999",
        shares_outstanding: "999",
        vintage: { gross_profit: FILING("9999999999-99-999999", "2099-12-31") },
      },
      close: "999",
      financialKnowableAt: "2026-04-15T00:00:00Z",
      priceKnowableAt: "2026-04-15T00:00:00Z",
    });

    await admin.query(
      `insert into mart.strategy_runs
         (strategy_run_id, content_sha256, strategy_key, strategy_version, definition_content_sha256,
          corpus_sha256, claim_ceiling, executed_at)
       values ($1, $2, 'large_model_value_v0', 'v0', $3, $4, 'preview', '2999-12-31T00:00:00Z')`,
      [strategyRunId, hex(), hex(), hex()],
    );
    await admin.query(
      `insert into mart.strategy_decisions
         (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
          capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
          valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight)
       values ($1, $2, $3, $4, $5, '12.5', 'tech', '20', '18.75', '0.5', true, 'selected', null, 1, '1.0')`,
      [`strategy-decision:${hex()}`, hex(), strategyRunId, entityId, CUTOFF],
    );
    await admin.query(
      `insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by)
       values ($1, $2, 'tick')`,
      [boundCaptureRun, strategyRunId],
    );

    // --- the deployed path: loadTrace with the default adapter, as /research/trace calls it ---
    const state = await loadTrace(CONTEXT, legacyId, CUTOFF);
    assert(state.kind === "ready", `expected a ready trace, got ${JSON.stringify(state)}`);
    const { inputs } = state.data;
    assert(inputs.length === 6, `expected the six inputs, got ${inputs.length}`);
    assert(state.data.lineageResultId === bound.resultId, `the lineage must come from the bound run's result: ${state.data.lineageResultId}`);

    const grossProfit = input(inputs, "gross_profit");
    assert(grossProfit.value === "210000000", `gross_profit value: ${grossProfit.value}`);
    assert(grossProfit.period === "2025-12-31", `gross_profit period: ${grossProfit.period}`);
    assert(grossProfit.accession === "0000320193-26-000010", `gross_profit accession: ${grossProfit.accession}`);
    assert(instant(grossProfit.knowableAt) === "2026-02-01T00:00:00.000Z", `gross_profit knowable_at: ${grossProfit.knowableAt}`);
    assert(grossProfit.sourceVintageId === bound.financial.sourceVintageId, `gross_profit vintage: ${grossProfit.sourceVintageId}`);

    assert(input(inputs, "total_assets").value === "200000000", "total_assets value");
    assert(input(inputs, "revenue").value === "100000000", "revenue value");
    assert(input(inputs, "revenue").accession === "0000320193-26-000011", "revenue names its own filing");
    assert(input(inputs, "revenue").period === "2025-09-30", "revenue names its own period");
    assert(input(inputs, "shares_outstanding").value === "10000000", "shares value");
    assert(input(inputs, "shares_outstanding").period === "2025-10-17", "shares period");

    const headcount = input(inputs, "headcount");
    assert(headcount.value === "100" && headcount.period === "2025-06-30", "headcount value and period");
    assert(headcount.accession === null, "a headcount with no filing has no accession; it is not invented");

    const price = input(inputs, "close");
    assert(price.value === "40", `price value: ${price.value}`);
    assert(instant(price.knowableAt) === "2026-03-30T21:00:00.000Z", `price knowable_at: ${price.knowableAt}`);
    assert(price.sourceVintageId === bound.price.sourceVintageId, "price carries its own source vintage");
    assert(price.accession === null && price.period === null, "a price has no accession and no period");

    // The decoy's values appear nowhere. Whole-field comparison: the source vintage ids are random hex,
    // and a substring test on them would pass or fail by chance.
    assert(
      inputs.every((candidate) => candidate.value !== "999" && candidate.accession !== "9999999999-99-999999"),
      "the decoy run's values reached the trace",
    );

    // An issuer and cutoff that no decision holds is an empty trace, not a lineage read.
    const missing = await loadTrace(CONTEXT, legacyId, "2026-01-01T00:00:00Z");
    assert(missing.kind === "empty", `an unknown cutoff must be empty, got ${missing.kind}`);
    assert(NOT_RECORDED === "not recorded", "the page text for a missing field is part of the contract");

    console.log("#1241 trace lineage through loadTrace on a real schema passed");
  } finally {
    __setTestClient(null);
    await admin.query("rollback").catch(() => {});
    await admin.end().catch(() => {});
  }
}
