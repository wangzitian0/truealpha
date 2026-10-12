/**
 * #1241: the trace page reads the lineage of a decision, through the server function the deployed
 * page calls (`loadTrace`), against a real Postgres. It skips without one and fails when armed
 * (ci-web provides the database and TRUEALPHA_REQUIRE_RUNTIME=1).
 *
 * The seed has two phases, each in its own rolled-back transaction.
 *
 * Phase 1, the governed head. A pointer names the capture run of strategy run G, so
 * `mart.served_head` resolves it. A newer strategy run N has no head. The page must show G, and
 * must say it is governed. The bound capture run holds:
 *   - issuer 1 at cutoff 1 and at cutoff 2, one listing each, other values at each cutoff;
 *   - issuer 2 at cutoff 1 with two listings (dual-class): other shares and price for each listing;
 *   - a decision of issuer 1 at cutoff 3 with no core result.
 * An unbound capture run holds a newer decoy result for issuer 1 at cutoff 1. Result ids are chosen
 * so that a read which drops the issuer predicate, the cutoff predicate or the capture-run binding
 * finds a wrong row first and fails.
 *
 * Phase 2, the fallback. No pointer exists. The newest recorded run is shown, and the page must say
 * it is not the governed head.
 *
 * Each phase runs on the test's own client, lent to the read layer through db.ts's
 * `__setTestClient` seam, and is rolled back. `session_replication_role = replica` turns off the
 * foreign keys and the insert triggers for the transaction, so the seed holds the rows the view
 * reads and not the whole capture pipeline. CHECK constraints stay on.
 * Run standalone: `ALLOW_CLEAR_TEST=1 DATABASE_URL=... bun run tests/trace-lineage-mart.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Client, type PoolClient } from "pg";

import type { AccessContext } from "../src/contracts/strategyRun";
import { GovernedRunNotice } from "../src/components/trace-inputs-table";
import { loadTrace } from "../src/server/dashboard";
import { __setTestClient } from "../src/server/mart/db";
import type { TraceInput, TraceListing, TraceView } from "../src/server/mart/research-read";

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
const CUTOFF_1 = "2026-03-31T00:00:00Z";
const CUTOFF_2 = "2026-02-28T00:00:00Z";
const CUTOFF_3 = "2026-01-31T00:00:00Z";

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

interface ListingSeed {
  listingId: string;
  instrumentId: string;
  shares: string;
  close: string;
}

interface SeededListing {
  financial: Chain;
  price: Chain;
}

/** One issuer's core result in one capture run, with the four observations of each of its listings. */
async function seedResult(
  db: Client,
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

/** An issuer: its entity id (what the mart holds) and its legacy id (what the page shows). */
async function seedIssuer(db: Client, prefix: string): Promise<{ entityId: string; legacyId: string }> {
  const suffix = hex(6);
  const entityId = `${prefix}-${prefix.slice(0, 4)}-5${prefix.slice(1, 4)}-8${prefix.slice(1, 4)}-${suffix}`;
  const legacyId = `issuer:lei:TRACE${suffix.toUpperCase()}`;
  await db.query(
    `insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value)
     values ($1, 'issuer', 'uuidv5:v1', 'trace-test', 'lei', $2)`,
    [entityId, legacyId],
  );
  await db.query(
    `insert into staging.entity_aliases
       (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
     values ($1, 'legacy-id', $2, '2026-01-01', '2026-01-01T00:00:00Z', 'trace-test', 'trace-test', 'asserted', 1, 'trace-test:v1')`,
    [entityId, legacyId],
  );
  return { entityId, legacyId };
}

async function seedStrategyRun(
  db: Client,
  args: {
    strategyRunId: string;
    captureRunId: string;
    executedAt: string;
    decisions: Array<{ entityId: string; cutoff: string }>;
  },
): Promise<void> {
  await db.query(
    `insert into mart.strategy_runs
       (strategy_run_id, content_sha256, strategy_key, strategy_version, definition_content_sha256,
        corpus_sha256, claim_ceiling, executed_at)
     values ($1, $2, 'large_model_value_v0', 'v0', $3, $4, 'preview', $5)`,
    [args.strategyRunId, hex(), hex(), hex(), args.executedAt],
  );
  for (const decision of args.decisions) {
    await db.query(
      `insert into mart.strategy_decisions
         (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
          capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
          valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight)
       values ($1, $2, $3, $4, $5, '12.5', 'tech', '20', '18.75', '0.5', true, 'selected', null, 1, '1.0')`,
      [`strategy-decision:${hex()}`, hex(), args.strategyRunId, decision.entityId, decision.cutoff],
    );
  }
  await db.query(
    `insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by)
     values ($1, $2, 'tick')`,
    [args.captureRunId, args.strategyRunId],
  );
}

function input(listing: TraceListing, key: string): TraceInput {
  const found = listing.inputs.find((candidate) => candidate.key === key);
  assert(found !== undefined, `no input ${key}`);
  return found;
}

function listingNamed(trace: TraceView, listingId: string): TraceListing {
  const found = trace.listings.find((candidate) => candidate.listingId === listingId);
  assert(found !== undefined, `no listing ${listingId} in ${trace.listings.map((l) => l.listingId).join(",")}`);
  return found;
}

function instant(text: string | null): string | null {
  return text === null ? null : new Date(text).toISOString();
}

async function readTrace(legacyId: string, cutoff: string): Promise<TraceView> {
  const state = await loadTrace(CONTEXT, legacyId, cutoff);
  assert(state.kind === "ready", `expected a ready trace for ${legacyId} @ ${cutoff}, got ${JSON.stringify(state)}`);
  return state.data;
}

/** One rolled-back transaction with the replica role and the test client seam. */
async function inTransaction(admin: Client, body: () => Promise<void>): Promise<void> {
  await admin.query("begin");
  try {
    await admin.query("set local session_replication_role = replica");
    __setTestClient(admin as unknown as Pick<PoolClient, "query">);
    await body();
  } finally {
    __setTestClient(null);
    await admin.query("rollback").catch(() => {});
  }
}

const SINGLE = (suffix: string): ListingSeed[] => [
  { listingId: `listing:xnas:${suffix}`, instrumentId: `instrument:${suffix}`, shares: "10000000", close: "40" },
];

const admin = await reachable();
if (admin !== null) {
  try {
    // ─── Phase 1: the governed head ─────────────────────────────────────────────────────────────
    await inTransaction(admin, async () => {
      const issuer1 = await seedIssuer(admin, "11111111");
      const issuer2 = await seedIssuer(admin, "22222222");
      const governedStrategy = `strategy-run:${hex()}`;
      const newerStrategy = `strategy-run:${hex()}`;
      const boundCapture = `capture-run:${hex()}`;
      const newerCapture = `capture-run:${hex()}`;
      const decoyCapture = `capture-run:${hex()}`;
      const single1 = SINGLE("one");
      const dual = [
        { listingId: "listing:xnas:goog", instrumentId: "instrument:goog", shares: "10000000", close: "40" },
        { listingId: "listing:xnas:googl", instrumentId: "instrument:googl", shares: "6000000", close: "55.5" },
      ];

      // The bound capture run. The result ids sort so that each dropped predicate finds a wrong row first:
      // issuer 2 (11…) before issuer 1, and issuer 1 at cutoff 2 (22…) before issuer 1 at cutoff 1 (ff…).
      const r1c1 = await seedResult(admin, {
        captureRunId: boundCapture, entityId: issuer1.entityId, cutoff: CUTOFF_1, resultHashPrefix: "ff",
        createdAt: "2026-04-01T00:00:00Z", grossProfit: "210000000", listings: single1,
      });
      const r1c2 = await seedResult(admin, {
        captureRunId: boundCapture, entityId: issuer1.entityId, cutoff: CUTOFF_2, resultHashPrefix: "22",
        createdAt: "2026-03-01T00:00:00Z", grossProfit: "111000000", listings: single1,
      });
      const r2c1 = await seedResult(admin, {
        captureRunId: boundCapture, entityId: issuer2.entityId, cutoff: CUTOFF_1, resultHashPrefix: "11",
        createdAt: "2026-04-01T00:00:00Z", grossProfit: "330000000", listings: dual,
      });
      // The decoy: issuer 1 at cutoff 1, newer, other values, a capture run that no strategy run is bound to.
      await seedResult(admin, {
        captureRunId: decoyCapture, entityId: issuer1.entityId, cutoff: CUTOFF_1, resultHashPrefix: "00",
        createdAt: "2026-05-01T00:00:00Z", grossProfit: "999", listings: single1,
      });
      // The newer strategy run N: no head. It must lose to the governed run.
      await seedResult(admin, {
        captureRunId: newerCapture, entityId: issuer1.entityId, cutoff: CUTOFF_1, resultHashPrefix: "ee",
        createdAt: "2026-06-01T00:00:00Z", grossProfit: "777000000", listings: single1,
      });

      await seedStrategyRun(admin, {
        strategyRunId: governedStrategy, captureRunId: boundCapture, executedAt: "2999-12-30T00:00:00Z",
        decisions: [
          { entityId: issuer1.entityId, cutoff: CUTOFF_1 },
          { entityId: issuer1.entityId, cutoff: CUTOFF_2 },
          { entityId: issuer1.entityId, cutoff: CUTOFF_3 },
          { entityId: issuer2.entityId, cutoff: CUTOFF_1 },
        ],
      });
      await seedStrategyRun(admin, {
        strategyRunId: newerStrategy, captureRunId: newerCapture, executedAt: "2999-12-31T00:00:00Z",
        decisions: [{ entityId: issuer1.entityId, cutoff: CUTOFF_1 }],
      });

      // The governed head: a pointer to the bound capture run, in this database's own environment.
      const pointerSha = hex();
      await admin.query(
        `insert into mart.current_pointer
           (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
            target_run_id, sequence, previous_run_id, advanced_at)
         values ($1, $2, (select environment from mart.environment_identity), $3, 'v1',
                 'gross_profit_per_employee', $4, 0, null, now())`,
        [`current-pointer:${pointerSha}`, pointerSha, `universe:topt-trace-${hex(4)}`, boundCapture],
      );
      const head = await admin.query("select run_id from mart.served_head where run_id = $1", [boundCapture]);
      assert(head.rows.length === 1, "the seed must give mart.served_head a head for the bound capture run");

      // --- issuer 1, cutoff 1: the governed run's own lineage, not the decoy and not the newer run ---
      const trace1 = await readTrace(issuer1.legacyId, CUTOFF_1);
      assert(trace1.governed === true, `the governed head resolves this run: governed=${trace1.governed}`);
      assert(trace1.lineageResultId === r1c1.resultId, `lineage from the bound run's result: ${trace1.lineageResultId}`);
      assert(trace1.listings.length === 1, `one listing: ${trace1.listings.length}`);
      const [listing1] = trace1.listings;
      assert(listing1.listingId === single1[0].listingId && listing1.isResultListing, "the set is labeled with its listing");
      const seeded1 = r1c1.byListing.get(single1[0].listingId);
      assert(seeded1 !== undefined, "seeded listing");

      const grossProfit = input(listing1, "gross_profit");
      assert(grossProfit.value === "210000000" && grossProfit.currency === "USD", `gross_profit: ${grossProfit.value} ${grossProfit.currency}`);
      assert(grossProfit.period === "2025-12-31", `gross_profit period: ${grossProfit.period}`);
      assert(grossProfit.accession === "0000320193-26-000010", `gross_profit accession: ${grossProfit.accession}`);
      assert(grossProfit.knowableAt === "2026-02-01" && grossProfit.knowableAtBasis === "filed", `gross_profit time: ${grossProfit.knowableAt}`);
      assert(grossProfit.source === seeded1.financial.sourceVintageId, `gross_profit source: ${grossProfit.source}`);
      assert(input(listing1, "total_assets").value === "200000000", "total_assets value");
      assert(input(listing1, "revenue").accession === "0000320193-26-000011", "revenue names its own filing");
      assert(input(listing1, "revenue").knowableAt === "2026-01-15", "revenue names its own filing date");
      assert(input(listing1, "shares_outstanding").value === "10000000", "shares value");

      // The headcount names its own pointer, never the filing facts' source vintage.
      const headcount = input(listing1, "headcount");
      assert(headcount.value === "100" && headcount.period === "2025-06-30", "headcount value and period");
      assert(headcount.accession === null, "a headcount with no filing has no accession; it is not invented");
      assert(headcount.source === "raw.fetches:7" && headcount.sourceKind === "raw pointer", `headcount source: ${headcount.source}`);
      assert(headcount.source !== seeded1.financial.sourceVintageId, "the headcount must not carry the filing facts' source vintage");
      assert(headcount.knowableAtBasis === "input" && instant(headcount.knowableAt) === "2026-01-20T00:00:00.000Z", `headcount time: ${headcount.knowableAt}`);

      const price = input(listing1, "close");
      assert(price.value === "40" && price.currency === "USD", `price: ${price.value} ${price.currency}`);
      assert(price.knowableAtBasis === "observation" && instant(price.knowableAt) === "2026-03-30T21:00:00.000Z", `price time: ${price.knowableAt}`);
      assert(price.source === seeded1.price.sourceVintageId, "price carries its own source vintage");
      assert(price.accession === null && price.period === null, "a price has no accession and no period");
      assert(
        trace1.listings.every((l) => l.inputs.every((i) => i.value !== "999" && i.value !== "777000000" && i.value !== "111000000")),
        "a decoy, newer-run or other-cutoff value reached the trace",
      );

      // --- issuer 1, cutoff 2: the result of this cutoff, in the same capture run ---
      const trace2 = await readTrace(issuer1.legacyId, CUTOFF_2);
      assert(trace2.lineageResultId === r1c2.resultId, `cutoff 2 reads its own result: ${trace2.lineageResultId}`);
      assert(input(trace2.listings[0], "gross_profit").value === "111000000", "cutoff 2 shows its own gross profit");

      // --- issuer 1, cutoff 3: a decision with no core result in the run. Nothing is recorded ---
      const trace3 = await readTrace(issuer1.legacyId, CUTOFF_3);
      assert(trace3.lineageResultId === null, `cutoff 3 has no core result: ${trace3.lineageResultId}`);
      assert(
        trace3.listings.length === 1 && trace3.listings[0].inputs.every((i) => i.value === null && i.accession === null && i.source === null),
        "a decision with no core result records nothing; another cutoff's values must not stand in",
      );

      // --- issuer 2: two listings of one issuer, each with its own shares and price ---
      const trace4 = await readTrace(issuer2.legacyId, CUTOFF_1);
      assert(trace4.lineageResultId === r2c1.resultId, `issuer 2 reads its own result: ${trace4.lineageResultId}`);
      assert(trace4.listings.length === 2, `two listings of one issuer: ${trace4.listings.length}`);
      assert(trace4.listings[0].listingId === "listing:xnas:goog" && trace4.listings[0].isResultListing, "the result's listing leads");
      const goog = listingNamed(trace4, "listing:xnas:goog");
      const googl = listingNamed(trace4, "listing:xnas:googl");
      assert(input(goog, "shares_outstanding").value === "10000000" && input(goog, "close").value === "40", "GOOG shares and close under GOOG");
      assert(input(googl, "shares_outstanding").value === "6000000" && input(googl, "close").value === "55.5", "GOOGL shares and close under GOOGL");
      const seededGoog = r2c1.byListing.get("listing:xnas:goog");
      const seededGoogl = r2c1.byListing.get("listing:xnas:googl");
      assert(seededGoog !== undefined && seededGoogl !== undefined, "seeded listings");
      assert(input(goog, "close").source === seededGoog.price.sourceVintageId, "GOOG close names GOOG's price vintage");
      assert(input(googl, "close").source === seededGoogl.price.sourceVintageId, "GOOGL close names GOOGL's price vintage");
      assert(input(googl, "gross_profit").source === seededGoogl.financial.sourceVintageId, "GOOGL gross profit names GOOGL's financial vintage");

      // An issuer and cutoff that no decision holds is an empty trace, not a lineage read.
      const unknown = await loadTrace(CONTEXT, issuer1.legacyId, "2025-01-01T00:00:00Z");
      assert(unknown.kind === "empty", `an unknown cutoff must be empty, got ${unknown.kind}`);

      // A governed run needs no note.
      assert(renderToStaticMarkup(createElement(GovernedRunNotice, { governed: trace1.governed })) === "", "a governed run needs no note");
    });

    // ─── Phase 2: no head. The newest recorded run is shown and the page says so ─────────────────
    await inTransaction(admin, async () => {
      const issuer = await seedIssuer(admin, "33333333");
      const strategyRun = `strategy-run:${hex()}`;
      const captureRun = `capture-run:${hex()}`;
      const listings = SINGLE("fallback");
      const result = await seedResult(admin, {
        captureRunId: captureRun, entityId: issuer.entityId, cutoff: CUTOFF_1, resultHashPrefix: "ab",
        createdAt: "2026-04-01T00:00:00Z", grossProfit: "555000000", listings,
      });
      await seedStrategyRun(admin, {
        strategyRunId: strategyRun, captureRunId: captureRun, executedAt: "2999-12-31T12:00:00Z",
        decisions: [{ entityId: issuer.entityId, cutoff: CUTOFF_1 }],
      });
      const heads = await admin.query("select 1 from mart.served_head where run_id = $1", [captureRun]);
      assert(heads.rows.length === 0, "phase 2 must have no head for its capture run");

      const trace = await readTrace(issuer.legacyId, CUTOFF_1);
      assert(trace.governed === false, `no head resolves the run: governed=${trace.governed}`);
      assert(trace.lineageResultId === result.resultId, `the fallback run's own lineage: ${trace.lineageResultId}`);
      assert(input(trace.listings[0], "gross_profit").value === "555000000", "the fallback run's values are shown");
      const note = renderToStaticMarkup(createElement(GovernedRunNotice, { governed: trace.governed }));
      assert(note.includes("not the governed head"), `the page must say the run is not the governed head: ${note}`);
    });

    console.log("#1241 trace lineage through loadTrace on a real schema passed");
  } finally {
    __setTestClient(null);
    await admin.end().catch(() => {});
  }
}
