/**
 * #539: the universes loader — heads dedupe to the newest sequence per kind,
 * members ride with raw lineage. Fake client via the ops test override.
 *
 * Run standalone: `bun run tests/admin-universes.test.ts`.
 */

import { laneNames } from "../src/contracts/universes";
import { __setTestOpsClient } from "../src/server/admin/ops";
import { loadUniverses } from "../src/server/admin/universes";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function bindsLaneNames(params: readonly unknown[] | undefined): boolean {
  return JSON.stringify(params?.[0]) === JSON.stringify(laneNames());
}

const versionRows = [
  { kind: "universe-list:qqq", sequence: 2, note: "auto refresh", advanced_at: "2026-08-17", universe_id: "universe:qqq-us-2026-06-30", instrument_count: 102, mapping_sha256: "b".repeat(64) },
  { kind: "universe-list:qqq", sequence: 1, note: "first publish", advanced_at: "2026-08-10", universe_id: "universe:qqq-us-2026-06-30", instrument_count: 101, mapping_sha256: "a".repeat(64) },
  { kind: "universe-list:sp500", sequence: 1, note: "sp500 publish", advanced_at: "2026-08-17", universe_id: "universe:sp500-us-2026-06-30", instrument_count: 503, mapping_sha256: "d".repeat(64) },
];
const memberRowsByEtf: Record<string, unknown[]> = {
  qqq: [
    { ticker: "AAPL", company_name: "Apple Inc. Common Stock", cik: 320193, figi: "bbg001s5n8v8", market_cap: "4464797487400", as_of: "2026-08-17", source: "nasdaq-index", fetch_sha256: "c".repeat(64), object_uri: "s3://truealpha-raw/raw/nasdaq-index/cc/x" },
  ],
  sp500: [
    { ticker: "BRK.B", company_name: "Berkshire Hathaway Inc.", cik: 1067983, figi: "bbg000dwd2cd", market_cap: "980000000000", as_of: "2026-08-17", source: "sp-index", fetch_sha256: "e".repeat(64), object_uri: "s3://truealpha-raw/raw/sp-index/ee/y" },
  ],
};

{
  const roleQueries: string[] = [];
  const memberQueries: { sql: string; params: readonly unknown[] | undefined }[] = [];
  let versionParams: readonly unknown[] | undefined;
  __setTestOpsClient({
    query: async (sql: string, params?: readonly unknown[]) => {
      if (sql === "set role app_ops_reader" || sql === "reset role") {
        roleQueries.push(sql);
        return { rows: [] } as never;
      }
      if (typeof sql === "string" && sql.includes("from staging.accepted_rulesets")) {
        versionParams = params;
        return { rows: versionRows } as never;
      }
      if (typeof sql === "string" && sql.includes("from staging.etf_constituent_facts")) {
        memberQueries.push({ sql, params });
        const etf = String(params?.[0] ?? "");
        return { rows: memberRowsByEtf[etf] ?? [] } as never;
      }
      throw new Error(`unexpected query: ${sql}`);
    },
  } as never);
  const overview = await loadUniverses();
  assert(
    roleQueries[0] === "set role app_ops_reader" && roleQueries[1] === "reset role",
    "loadUniverses runs inside app_ops_reader role scope",
  );
  assert(
    bindsLaneNames(versionParams),
    "the versions query selects the kinds that are lane names of the universe record",
  );
  assert(overview.heads.length === 2, "one head per kind across multiple kinds");
  assert(overview.heads[0].kind === "universe-list:qqq" && overview.heads[0].sequence === 2, "qqq head is newest sequence");
  assert(overview.heads[1].kind === "universe-list:sp500" && overview.heads[1].sequence === 1, "sp500 head is preserved");
  assert(overview.history.length === 3, "history keeps every version");
  assert(
    memberQueries.length === 2 &&
      memberQueries[0].params?.[0] === "qqq" &&
      memberQueries[1].params?.[0] === "sp500" &&
      memberQueries[0].sql.includes("where f.etf_symbol = $1"),
    "MEMBERS_SQL runs once per deduped head with stripped ETF symbol as $1",
  );
  assert(overview.members["universe-list:qqq"][0].fetch_sha256 === "c".repeat(64), "qqq members carry raw lineage");
  assert(overview.members["universe-list:sp500"][0].ticker === "BRK.B", "sp500 members map by head kind");
  __setTestOpsClient(null);
}

// A head kind outside the record throws; it never maps to another universe (#1522).
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql === "set role app_ops_reader" || sql === "reset role") return { rows: [] } as never;
      if (typeof sql === "string" && sql.includes("from staging.accepted_rulesets")) {
        return { rows: [{ ...versionRows[0], kind: "universe-list:nasdaq100" }] } as never;
      }
      throw new Error(`unexpected query: ${sql}`);
    },
  } as never);
  let failure: unknown = null;
  try {
    await loadUniverses();
  } catch (error) {
    failure = error;
  }
  assert(
    failure instanceof Error && failure.message.includes("lane name of no universe"),
    "a head kind outside the record must throw",
  );
  __setTestOpsClient(null);
}

console.log("#539 admin universes loader passed");
