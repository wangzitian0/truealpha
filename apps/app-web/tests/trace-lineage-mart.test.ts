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
 * The rows of one core result and its lineage come from `scripts/lineage-seed.ts`. The e2e seed
 * `scripts/seed-e2e-fixture.ts` uses the same helper.
 * Run standalone: `ALLOW_CLEAR_TEST=1 DATABASE_URL=... bun run tests/trace-lineage-mart.test.ts`.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Client, type PoolClient } from "pg";

import type { AccessContext } from "../src/contracts/strategyRun";
import { GovernedRunNotice } from "../src/components/trace-inputs-table";
import { loadTrace } from "../src/server/dashboard";
import { __setTestClient } from "../src/server/mart/db";
import type { TraceInput, TraceListing, TraceView } from "../src/server/mart/research-read";
import { hex, seedResult, type ListingSeed } from "../scripts/lineage-seed";

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
      // Which listing carries the result flag is an identity check; the order case lives in
      // trace-lineage-inputs.test.ts, where the result listing does not sort first.
      const flagged = trace4.listings.filter((l) => l.isResultListing).map((l) => l.listingId);
      assert(flagged.length === 1 && flagged[0] === "listing:xnas:goog", `only the result listing is flagged: ${flagged}`);
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
