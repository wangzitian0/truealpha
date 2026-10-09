/**
 * The entity page's theme reader against a real Postgres (#1117).
 *
 * An issuer without a segment partition has a fill row for each theme: NULL in every partition
 * column, extractor `lane:unvisited:v1`, and the reason in `reason_codes`. The reader takes the
 * newest row per theme. It must show a fill as "no answer, and why", never as a refused real row,
 * and a newer fill must not hide an older real row.
 *
 * Skips gracefully without a database; throws when armed (ci-web sets DATABASE_URL and
 * TRUEALPHA_REQUIRE_RUNTIME=1). All rows live in ONE transaction on the test's own client, lent
 * to the reader through db.ts's `__setTestClient`, and the transaction is rolled back.
 * Run standalone: `bun run tests/mart-entity-theme-purity.test.ts`.
 */

import { randomUUID } from "node:crypto";

import { Client, type PoolClient } from "pg";

import type { AccessContext } from "../src/contracts/strategyRun";
import { __setTestClient } from "../src/server/mart/db";
import { StrategyRunReadAdapter } from "../src/server/mart/research-read";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);
process.env.DATABASE_URL ??= "postgresql://postgres:postgres@localhost:5432/truealpha";

const CONTEXT: AccessContext = {
  contextId: "ctx:themes",
  principalId: "principal:themes",
  tenantId: "tenant:themes",
  sessionId: "session:themes",
  authenticationMethod: "service_identity",
  issuedAt: "2026-07-23T00:00:00Z",
  expiresAt: "2026-07-23T01:00:00Z",
};

const OLD_CUTOFF = "2026-01-01T00:00:00Z";
const NEW_CUTOFF = "2026-02-01T00:00:00Z";

const THEME_ROW_SQL = `insert into mart.issuer_theme_purity
 (run_id, issuer_id, cik, theme_id, theme, definition_version, definition_sha256, cutoff,
  period_end, partition_id, theme_share, consolidated_revenue, in_theme_revenue,
  out_of_theme_revenue, unclassified_revenue, partition_residual, segments, confidence,
  reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status)
 values ($1, $2, $3, $4, $5, 'v0', $6, $7, $8, $9, $10, $11, $12, $13, $14, $14, $15, $16, $17, $18, $19, $20,
         'not_evaluated')`;

/** A judged share of 0.5, or the fill row of an issuer without a segment partition. */
async function themeRow(
  client: Client,
  options: { issuerId: string; themeId: string; cutoff: string; fill: boolean },
): Promise<void> {
  const { issuerId, themeId, cutoff, fill } = options;
  await client.query(THEME_ROW_SQL, [
    `run:themes-1117:${cutoff}`,
    issuerId,
    fill ? null : 4242,
    themeId,
    themeId.toUpperCase(),
    "a".repeat(64),
    cutoff,
    fill ? null : "2025-12-31",
    fill ? null : `segment-partition:${"b".repeat(64)}`,
    fill ? null : "0.5",
    fill ? null : "100",
    fill ? null : "50",
    fill ? null : "50",
    fill ? null : "0",
    fill ? 0 : 2,
    fill ? "0" : "0.9",
    fill ? ["no_segment_partition"] : [],
    fill ? "lane:unvisited:v1" : "rule:test",
    fill ? "unavailable" : "available",
    fill ? "degraded" : "verified",
  ]);
}

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: process.env.DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("mart-entity-theme-purity: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

const admin = await reachable();
if (admin !== null) {
  try {
    await admin.query("begin");
    __setTestClient(admin as unknown as Pick<PoolClient, "query">);
    const adapter = new StrategyRunReadAdapter();

    // An issuer with a fill and nothing else: each theme says why it has no answer.
    {
      const issuerId = randomUUID();
      for (const themeId of ["zz-theme-a", "zz-theme-b"]) {
        await themeRow(admin, { issuerId, themeId, cutoff: NEW_CUTOFF, fill: true });
      }
      const themes = await adapter.entityThemePurity(CONTEXT, issuerId);
      assert(themes.length === 2, `one entry per theme, got ${JSON.stringify(themes)}`);
      for (const theme of themes) {
        assert(theme.unvisitedReason === "no_segment_partition", `a fill names its reason: ${JSON.stringify(theme)}`);
        assert(theme.availabilityStatus === "unavailable", "a fill is unavailable");
        assert(theme.themeShare === null && theme.inThemeRevenue === null, "a fill has no share and no revenue");
        assert(theme.consolidatedRevenue === null, "a fill has no denominator");
        assert(theme.segments === null && theme.confidence === null, "a fill has no segment count and no confidence");
      }
    }

    // A newer fill must not hide an older real row. A theme with only a fill still says why.
    {
      const issuerId = randomUUID();
      await themeRow(admin, { issuerId, themeId: "zz-theme-a", cutoff: OLD_CUTOFF, fill: false });
      await themeRow(admin, { issuerId, themeId: "zz-theme-a", cutoff: NEW_CUTOFF, fill: true });
      await themeRow(admin, { issuerId, themeId: "zz-theme-b", cutoff: NEW_CUTOFF, fill: true });
      const themes = await adapter.entityThemePurity(CONTEXT, issuerId);
      const byId = new Map(themes.map((theme) => [theme.themeId, theme]));
      const real = byId.get("zz-theme-a");
      assert(real !== undefined, "the theme with a real row is listed");
      assert(real.themeShare === "0.5", `the newer fill hid the real row: ${JSON.stringify(real)}`);
      assert(real.unvisitedReason === null, "a real row has no unvisited reason");
      assert(real.segments === 2 && real.confidence === "0.9", "a real row keeps its segment count and confidence");
      assert(byId.get("zz-theme-b")?.unvisitedReason === "no_segment_partition", "the other theme names its reason");
    }

    console.log("mart-entity-theme-purity: ok");
  } finally {
    __setTestClient(null);
    await admin.query("rollback").catch(() => {});
    await admin.end();
  }
}
