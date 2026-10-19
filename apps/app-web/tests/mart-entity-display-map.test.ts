/**
 * The display map of the research pages against a real Postgres (#1327).
 *
 * `loadEntityDisplayMap` joins `mart.entity_display_resolution` to `mart.entity_identity`.
 * The old statement used a lateral lookup. It ran the identity view's final join again for each
 * display row. On production, 234 display rows took 15.8 s to 30.7 s.
 *
 * This test seeds 61 display rows and checks four properties:
 *   1. The plan scans `staging.entities`, the driving table of the identity view, exactly once.
 *      The test reads the plan, not the clock, so a slow CI host cannot change the verdict.
 *   2. The statement returns one row for each display row, also when one entity matches twice.
 *   3. The loader keys each issuer by its UUID and by its legacy id. An issuer without an
 *      identity row keeps its ticker and gets no legacy key.
 *   4. When the identity rows of one entity disagree, the smallest legacy id wins and NULL loses.
 *      The real view repeats one legacy id on a duplicate row, so this check swaps in a stand-in
 *      view inside a savepoint.
 *
 * Throws when the database is unreachable (ci-web sets DATABASE_URL and TRUEALPHA_REQUIRE_RUNTIME=1).
 * All rows live in ONE transaction on the test's own client, lent to the loader through db.ts's
 * `__setTestClient`. The transaction is rolled back.
 * Run standalone: `CI=true DATABASE_URL=... bun run tests/mart-entity-display-map.test.ts`.
 */

import { randomUUID } from "node:crypto";

import { Client, type PoolClient } from "pg";

import { __setTestClient, withMartReadonly } from "../src/server/mart/db";
import { ENTITY_DISPLAY_SQL, loadEntityDisplayMap } from "../src/server/mart/entity-resolution";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const DATABASE_URL = requireDb("mart-entity-display-map");
if (!DATABASE_URL) {
  process.exit(0);
}
process.env.DATABASE_URL = DATABASE_URL;

/** The old statement scanned `staging.entities` once for each display row: 60 scans. One scan is correct. */
const ISSUER_COUNT = 60;

interface Issuer {
  entityId: string;
  legacyId: string;
  ticker: string;
  name: string;
}

interface DisplayRow {
  issuer_id: string;
  ticker: string;
  display_name: string | null;
  legacy_id: string | null;
}

interface PlanNode {
  "Node Type": string;
  Schema?: string;
  "Relation Name"?: string;
  "Actual Loops"?: number;
  Plans?: PlanNode[];
}

const MEMBER_SQL = `insert into staging.topt_core_snapshot_members
  (snapshot_id, instrument_id, issuer_id, listing_id, observation_ids, member_sha256, factor_input)
  values ('topt-core-snapshot:1327', $1, $2, $3, array['obs:1','obs:2','obs:3','obs:4'], $4, '{}'::jsonb)`;

/** A snapshot member: the row that makes `mart.entity_display_resolution` list an issuer. */
async function addMember(client: Client, issuerId: string, ticker: string): Promise<void> {
  const symbol = ticker.toLowerCase();
  await client.query(MEMBER_SQL, [`instrument:${symbol}`, issuerId, `listing:xnas:${symbol}`, "a".repeat(64)]);
}

/** Each issuer gets a minted entity, a `legacy-id` alias and a KG display name. */
async function seedIssuers(client: Client): Promise<Issuer[]> {
  const issuers: Issuer[] = [];
  for (let n = 1; n <= ISSUER_COUNT; n++) {
    const lei = `ZZ${String(n).padStart(16, "0")}00`;
    const minted = await client.query(
      "select staging.entity_mint('issuer', 'lei', $1, 'test:1327') as entity_id",
      [lei],
    );
    const issuer: Issuer = {
      entityId: String(minted.rows[0].entity_id),
      legacyId: `issuer:lei:${lei}`,
      ticker: `T${n}`,
      name: `Issuer ${n}`,
    };
    await client.query(
      `insert into staging.entity_aliases
         (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
       values ($1, 'legacy-id', $2, '2026-01-01', '2026-01-01T00:00:00Z', 'test:1327', 'test:1327', 'asserted', 1, 'test:1327')`,
      [issuer.entityId, issuer.legacyId],
    );
    await client.query("insert into staging.kg_entities (id, entity_type, display_name) values ($1, 'company', $2)", [
      issuer.entityId,
      issuer.name,
    ]);
    issuers.push(issuer);
  }
  return issuers;
}

function scansOf(node: PlanNode, schema: string, relation: string): PlanNode[] {
  const own = node.Schema === schema && node["Relation Name"] === relation ? [node] : [];
  return [...own, ...(node.Plans ?? []).flatMap((child) => scansOf(child, schema, relation))];
}

async function displayRows(): Promise<DisplayRow[]> {
  return withMartReadonly(async (client) => (await client.query(ENTITY_DISPLAY_SQL)).rows as DisplayRow[]);
}

/**
 * Replaces `mart.entity_identity` with a stand-in for the rest of the savepoint. The stand-in holds
 * only rows of `entityId`: one row for each value in `legacyIds`, in that order, differing only in
 * `legacy_id`. The entity must have one real identity row. Postgres sorts fewer than seven rows by
 * insertion, which keeps the physical order of equal keys. A pick without the tie-break rule then
 * returns the first row, which the caller makes the largest value.
 * `jsonb_populate_record` copies all other columns, so the stand-in follows the real view's columns.
 */
async function swapInDisagreeingIdentity(client: Client, entityId: string, legacyIds: (string | null)[]): Promise<void> {
  assert(/^[0-9a-f-]{36}$/.test(entityId), `not a UUID: ${entityId}`);
  const picks = legacyIds
    .map((legacyId, index) => `(${index + 1}, ${legacyId === null ? "null::text" : `'${legacyId}'`})`)
    .join(", ");
  await client.query("alter view mart.entity_identity rename to entity_identity_1327_real");
  await client.query(`create view mart.entity_identity as
    select (jsonb_populate_record(null::mart.entity_identity_1327_real,
                                  to_jsonb(t) || jsonb_build_object('legacy_id', pick.legacy_id))).*
    from mart.entity_identity_1327_real t
    cross join (values ${picks}) as pick(position, legacy_id)
    where t.entity_id = '${entityId}'
    order by pick.position`);
  await client.query("grant select on mart.entity_identity to mart_readonly");
  const held = (await client.query("select count(*)::int as n from mart.entity_identity")).rows[0].n;
  assert(held === legacyIds.length, `the stand-in holds ${held} rows, expected ${legacyIds.length}; the entity needs one real identity row`);
}

const admin = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
await admin.connect();
try {
  await admin.query("begin");
  __setTestClient(admin as unknown as Pick<PoolClient, "query">);

  const issuers = await seedIssuers(admin);
  const duplicate = issuers[0];
  // One more KG row, keyed by the legacy id, makes the identity view's KG join match this entity twice.
  await admin.query("insert into staging.kg_entities (id, entity_type, display_name) values ($1, 'company', $2)", [
    duplicate.legacyId,
    `${duplicate.name} (legacy-id row)`,
  ]);
  // An issuer that has no row in mart.entity_identity at all.
  const orphanId = randomUUID();
  // Replica mode turns triggers and foreign keys off, as seed-e2e-fixture.ts does. The member's own
  // trigger demands a full captured snapshot, which this test does not need.
  await admin.query("set local session_replication_role = replica");
  for (const issuer of issuers) await addMember(admin, issuer.entityId, issuer.ticker);
  await addMember(admin, orphanId, "ORPH");
  await admin.query("set local session_replication_role = origin");

  // Fresh statistics make the planner's row estimates match the seed. Autovacuum timing on a
  // shared CI database then cannot change the plan. The transaction rolls the statistics back.
  await admin.query(
    "analyze staging.entities, staging.entity_aliases, staging.entity_relations, staging.kg_entities, staging.topt_core_snapshot_members",
  );

  // Without these two preconditions, checks 2 and 3 could pass on a database that tests nothing.
  const displayCount: number = (await admin.query("select count(*)::int as n from mart.entity_display_resolution"))
    .rows[0].n;
  assert(displayCount >= ISSUER_COUNT + 1, `the seed produced ${displayCount} display rows, expected at least ${ISSUER_COUNT + 1}`);
  const duplicateIdentityRows: number = (
    await admin.query("select count(*)::int as n from mart.entity_identity where entity_id = $1", [duplicate.entityId])
  ).rows[0].n;
  assert(duplicateIdentityRows === 2, `the duplicate entity must match twice in the identity view, got ${duplicateIdentityRows}`);

  // 1. The identity view's driving table is scanned once, not once for each display row.
  const plan = await withMartReadonly(async (client) => {
    const result = await client.query(`explain (analyze, verbose, format json) ${ENTITY_DISPLAY_SQL}`);
    return (result.rows[0]["QUERY PLAN"] as { Plan: PlanNode }[])[0].Plan;
  });
  const scans = scansOf(plan, "staging", "entities");
  assert(scans.length > 0, "the plan has no scan of staging.entities, so the loops check would pass on nothing");
  for (const scan of scans) {
    assert(
      scan["Actual Loops"] === 1,
      `staging.entities was scanned ${scan["Actual Loops"]} times for ${displayCount} display rows; ` +
        "the identity view must be evaluated once",
    );
  }

  // 2. One row for each display row. A plain join would return the duplicate entity twice.
  const rows = await displayRows();
  assert(rows.length === displayCount, `${rows.length} statement rows for ${displayCount} display rows`);
  assert(new Set(rows.map((row) => row.issuer_id)).size === rows.length, "an issuer appears in two statement rows");
  const byIssuer = new Map(rows.map((row) => [row.issuer_id, row]));
  assert(byIssuer.get(duplicate.entityId)?.legacy_id === duplicate.legacyId, "the duplicate entity lost its legacy id");

  // 3. The loader keys each issuer twice. The orphan keeps its ticker and gets no legacy key.
  const map = await loadEntityDisplayMap();
  for (const issuer of issuers) {
    for (const key of [issuer.entityId, issuer.legacyId]) {
      const entry = map.get(key);
      assert(
        entry?.ticker === issuer.ticker && entry.displayName === issuer.name,
        `key ${key} did not resolve to ${issuer.ticker}: ${JSON.stringify(entry)}`,
      );
    }
  }
  const orphanRow = byIssuer.get(orphanId);
  assert(orphanRow !== undefined, "the issuer without an identity row vanished from the statement");
  assert(orphanRow.legacy_id === null, `the orphan got a legacy id: ${orphanRow.legacy_id}`);
  const orphan = map.get(orphanId);
  assert(orphan?.ticker === "ORPH" && orphan.displayName === null, `the orphan changed: ${JSON.stringify(orphan)}`);

  // 4. Identity rows that disagree on legacy_id: the smallest wins, and NULL loses.
  const largest = "issuer:lei:ZZZZZZZZZZZZZZZZZZ00";
  const smallest = "issuer:lei:AAAAAAAAAAAAAAAAAA00";
  // An entity with one real identity row, so the stand-in holds exactly three rows.
  const tied = issuers[1];
  await admin.query("savepoint tie_break");
  try {
    await swapInDisagreeingIdentity(admin, tied.entityId, [largest, null, smallest]);
    const tieRows = (await displayRows()).filter((row) => row.issuer_id === tied.entityId);
    assert(tieRows.length === 1, `${tieRows.length} statement rows for the entity with three identity rows`);
    assert(tieRows[0].legacy_id === smallest, `the smallest legacy id must win, got ${tieRows[0].legacy_id}`);
    const tieMap = await loadEntityDisplayMap();
    assert(tieMap.get(smallest)?.ticker === tied.ticker, "the winning legacy id does not resolve to the entity");
    assert(!tieMap.has(largest), "the losing legacy id still resolves");
  } finally {
    await admin.query("rollback to savepoint tie_break");
  }

  console.log("mart-entity-display-map: ok");
} finally {
  __setTestClient(null);
  await admin.query("rollback").catch(() => {});
  await admin.end();
}
