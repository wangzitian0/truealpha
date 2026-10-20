/**
 * #42: module 3 / module 4 parity, TypeScript half (against a real Postgres; skips without
 * one, throws when armed — ci-web provides the database and TRUEALPHA_REQUIRE_RUNTIME=1).
 *
 * Seeds common/contracts/conformance/issuer_modules_parity.json and asserts that the App's
 * `entitySupplyChainExposure` and `entityAnalystRatings` return the canon's expected rows.
 * The Python half (common/contracts/tests/test_issuer_modules_mart.py) asserts the same rows
 * for the reader behind the MCP tools and the research report: two languages, one served
 * row. The unserved run holds a newer, available decoy row, so a read that drops the
 * `mart.served_head` pin returns the decoy and fails here.
 *
 * Everything runs inside one transaction on the test's own client, lent to the adapter
 * through db.ts's `__setTestClient` seam and rolled back at the end.
 * Run standalone: `bun run tests/issuer-modules-parity-conformance.test.ts`.
 */

import { readFileSync } from "node:fs";

import { Client, type PoolClient } from "pg";

import type { AccessContext } from "../src/contracts/strategyRun";
import { __setTestClient } from "../src/server/mart/db";
import { moduleOutputCountSql, StrategyRunReadAdapter } from "../src/server/mart/research-read";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const dbUrl = requireDb("issuer-modules-parity-conformance", { write: true });
if (!dbUrl) {
  process.exit(0);
}
const DATABASE_URL: string = dbUrl;
process.env.DATABASE_URL = DATABASE_URL;
const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);

type ModuleRow = Record<string, unknown> & { run: "served" | "unserved"; cutoff: string };
interface Canon {
  issuer_id: string;
  served_run_id: string;
  unserved_run_id: string;
  pointer: { pointer_sha256: string; universe_id: string; universe_version: string; factor_id: string };
  supply_chain_exposure: { rows: ModuleRow[]; expected: Record<string, unknown> };
  analyst_ratings: { rows: ModuleRow[]; expected: Record<string, unknown> };
}

const CANON = JSON.parse(
  readFileSync(new URL("../../../common/contracts/conformance/issuer_modules_parity.json", import.meta.url), "utf8"),
) as Canon;

const CONTEXT: AccessContext = {
  contextId: "ctx:issuer-modules-parity",
  principalId: "principal:test",
  tenantId: "tenant:test",
  sessionId: "session:test",
  authenticationMethod: "service_identity",
  issuedAt: "2026-10-20T00:00:00Z",
  expiresAt: "2026-10-20T01:00:00Z",
};

const runId = (label: ModuleRow["run"]): string => (label === "served" ? CANON.served_run_id : CANON.unserved_run_id);

/** The App's camelCase detail object with the canon's snake_case keys, sorted, as JSON. */
function canonical(value: Record<string, unknown>, snakeCase: boolean): string {
  const entries = Object.entries(value).map(([key, item]) => [
    snakeCase ? key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`) : key,
    item,
  ]);
  entries.sort(([a], [b]) => String(a).localeCompare(String(b)));
  return JSON.stringify(Object.fromEntries(entries));
}

async function seedModuleRows(db: Client): Promise<void> {
  for (const row of CANON.supply_chain_exposure.rows) {
    await db.query(
      `insert into mart.issuer_supply_chain_exposure
         (run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count, customers_count,
          max_partner_share, confidence, reason_codes, extractor, availability_status,
          source_evidence_status, factor_validation_status)
       values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
      [
        runId(row.run), CANON.issuer_id, row.cutoff, row.exposure_score, row.direct_partners, row.suppliers_count,
        row.customers_count, row.max_partner_share, row.confidence, row.reason_codes, row.extractor,
        row.availability_status, row.source_evidence_status, row.factor_validation_status,
      ],
    );
  }
  for (const row of CANON.analyst_ratings.rows) {
    await db.query(
      `insert into mart.issuer_analyst_ratings
         (run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count,
          confidence, reason_codes, extractor, availability_status, source_evidence_status,
          factor_validation_status)
       values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
      [
        runId(row.run), CANON.issuer_id, row.cutoff, row.consensus_rating, row.analysts_count, row.buy_count,
        row.hold_count, row.sell_count, row.confidence, row.reason_codes, row.extractor, row.availability_status,
        row.source_evidence_status, row.factor_validation_status,
      ],
    );
  }
}

async function seedServedHead(db: Client): Promise<void> {
  const pointer = CANON.pointer;
  await db.query(
    `insert into mart.current_pointer
       (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
        target_run_id, sequence, previous_run_id, advanced_at)
     values ($1, $2, (select environment from mart.environment_identity), $3, $4, $5, $6, 0, null, now())`,
    [
      `current-pointer:${pointer.pointer_sha256}`, pointer.pointer_sha256, pointer.universe_id,
      pointer.universe_version, pointer.factor_id, CANON.served_run_id,
    ],
  );
  const head = await db.query("select 1 from mart.served_head where run_id = $1", [CANON.served_run_id]);
  assert(head.rows.length === 1, "the seed must give mart.served_head a head for the served run");
}

/** One rolled-back transaction with the replica role and the test client seam. */
async function inTransaction(db: Client, body: () => Promise<void>): Promise<void> {
  await db.query("begin");
  try {
    await db.query("set local session_replication_role = replica");
    __setTestClient(db as unknown as Pick<PoolClient, "query">);
    await body();
  } finally {
    __setTestClient(null);
    await db.query("rollback").catch(() => {});
  }
}

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("issuer-modules-parity-conformance: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

const db = await reachable();
if (db !== null) {
  try {
    const adapter = new StrategyRunReadAdapter();

    await inTransaction(db, async () => {
      await seedModuleRows(db);
      await seedServedHead(db);
      const supply = await adapter.entitySupplyChainExposure(CONTEXT, CANON.issuer_id);
      const analyst = await adapter.entityAnalystRatings(CONTEXT, CANON.issuer_id);
      assert(supply !== null, "the served supply-chain row was not read");
      assert(analyst !== null, "the served analyst row was not read");
      assert(
        canonical(supply as unknown as Record<string, unknown>, true) ===
          canonical(CANON.supply_chain_exposure.expected, false),
        `module 3 differs from the canon: ${JSON.stringify(supply)}`,
      );
      assert(
        canonical(analyst as unknown as Record<string, unknown>, true) ===
          canonical(CANON.analyst_ratings.expected, false),
        `module 4 differs from the canon: ${JSON.stringify(analyst)}`,
      );
      // The module coverage count describes the rows the entity page can show: never the
      // newer unserved run.
      for (const table of ["mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"] as const) {
        const counted = await db.query(moduleOutputCountSql(table));
        const runId = counted.rows[0]?.run_id;
        assert(runId !== CANON.unserved_run_id, `${table}: the coverage count names the unserved run ${runId}`);
        assert(typeof runId === "string", `${table}: the coverage count names no run`);
      }
    });

    // Rows exist, but no head serves their run: the App shows nothing, never a bare latest.
    await inTransaction(db, async () => {
      await seedModuleRows(db);
      const supply = await adapter.entitySupplyChainExposure(CONTEXT, CANON.issuer_id);
      const analyst = await adapter.entityAnalystRatings(CONTEXT, CANON.issuer_id);
      assert(supply === null, `an unserved supply-chain row was read: ${JSON.stringify(supply)}`);
      assert(analyst === null, `an unserved analyst row was read: ${JSON.stringify(analyst)}`);
    });

    console.log("issuer-modules-parity-conformance: the App reads the canon's served rows for modules 3 and 4");
  } finally {
    await db.end();
  }
}
