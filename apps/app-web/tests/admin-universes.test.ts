/**
 * #539: the universes loader — heads dedupe to the newest sequence per kind,
 * members ride with raw lineage. Fake client via the ops test override.
 *
 * Run standalone: `bun run tests/admin-universes.test.ts`.
 */

import { __setTestOpsClient } from "../src/server/admin/ops";
import { loadUniverses } from "../src/server/admin/universes";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const versionRows = [
  { kind: "universe-list:qqq", sequence: 2, note: "auto refresh", advanced_at: "2026-08-17", universe_id: "universe:qqq-us-2026-06-30", instrument_count: 102, mapping_sha256: "b".repeat(64) },
  { kind: "universe-list:qqq", sequence: 1, note: "first publish", advanced_at: "2026-08-10", universe_id: "universe:qqq-us-2026-06-30", instrument_count: 101, mapping_sha256: "a".repeat(64) },
];
const memberRows = [
  { ticker: "AAPL", company_name: "Apple Inc. Common Stock", cik: 320193, figi: "bbg001s5n8v8", market_cap: "4464797487400", as_of: "2026-08-17", source: "nasdaq-index", fetch_sha256: "c".repeat(64), object_uri: "s3://truealpha-raw/raw/nasdaq-index/cc/x" },
];

{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (typeof sql === "string" && sql.includes("accepted_rulesets")) return { rows: versionRows } as never;
      return { rows: memberRows } as never;
    },
  } as never);
  const overview = await loadUniverses();
  assert(overview.heads.length === 1, "one head per kind");
  assert(overview.heads[0].sequence === 2, "the head is the newest sequence");
  assert(overview.history.length === 2, "history keeps every version");
  assert(overview.members["universe-list:qqq"][0].fetch_sha256 === "c".repeat(64), "members carry raw lineage");
  __setTestOpsClient(null);
}

console.log("#539 admin universes loader passed");
