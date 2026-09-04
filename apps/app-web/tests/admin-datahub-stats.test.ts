/**
 * #641 D5: the datahub stats loader — heads carry both availabilities (the
 * capture-level headline and the #644 per-factor grades), sources and runs
 * pass through. Fake client via the ops test override.
 *
 * Run standalone: `bun run tests/admin-datahub-stats.test.ts`.
 */

import { loadDatahubStats } from "../src/server/admin/datahub-stats";
import { __setTestOpsClient } from "../src/server/admin/ops";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const headRows = [
  {
    universe_id: "universe:qqq-us-2026-06-30",
    sequence: 0,
    advanced_at: "2026-08-18 23:20:00+00",
    target_run_id: "capture-run:" + "9".repeat(64),
    availability: "0.7819",
    agreed_cells: 102,
    total_cells: 102,
    factor_availability: {
      gross_profit_per_employee: {
        required_semantics: ["financial-fact"],
        complete_subjects: 13,
        universe_subjects: 102,
        ratio: "0.1275",
      },
    },
  },
];
const sourceRows = [{ source: "yahoo", fetches_total: 728, fetches_24h: 123, last_fetch: "2026-08-18" }];
// #729: the external call ledger — one vendor today, and one request each way.
const trafficRows = [
  {
    source: "twelvedata",
    calls: 42,
    failed: 21,
    landed: 21,
    avg_ms: 310,
    last_call: "2026-09-05 22:19:44+00",
    last_error: "No data is available on the specified dates",
  },
];
const recentCallRows = [
  {
    id: 2,
    called_at: "2026-09-05 22:19:44+00",
    source: "twelvedata",
    endpoint: "time_series",
    caller: "twelve_data_origin",
    ok: true,
    status_code: 200,
    duration_ms: 290,
    error: null,
    run_key: "dagster:abc",
    landed_fetch_id: 6540,
  },
  {
    id: 1,
    called_at: "2026-09-05 22:19:33+00",
    source: "twelvedata",
    endpoint: "eod",
    caller: "twelve_data_origin",
    ok: false,
    status_code: 400,
    duration_ms: 330,
    error: "No data is available on the specified dates",
    run_key: "dagster:abc",
    landed_fetch_id: null,
  },
];
const runRows = [
  {
    run_id: "capture-run:" + "9".repeat(64),
    universe_id: "universe:qqq-us-2026-06-30",
    cutoff: "2026-08-18 23:20:00+00",
    obligations: 408,
    resolved: 408,
    failed: 0,
    complete: true,
  },
];

{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (typeof sql === "string" && sql.includes("current_pointer_head")) return { rows: headRows } as never;
      // "as check" FIRST: capacity SQLs also mention raw.fetches, and matching the
      // sources stub first fed them SourceStatRow shapes while the count-only
      // assertion stayed green (review on #678).
      if (typeof sql === "string" && sql.includes("as check")) {
        return { rows: [{ check: sql.slice(20, 44), verdict: "pass", detail: "stubbed" }] } as never;
      }
      // #729: the ledger reads also join raw.fetches, so they are matched BEFORE the
      // sources stub, on the ledger table itself; per-source aggregate vs recent list.
      if (typeof sql === "string" && sql.includes("api_call_ledger")) {
        return { rows: sql.includes("group by l.source") ? trafficRows : recentCallRows } as never;
      }
      if (typeof sql === "string" && sql.includes("raw.fetches")) return { rows: sourceRows } as never;
      return { rows: runRows } as never;
    },
  } as never);
  const stats = await loadDatahubStats();
  assert(stats.traffic.length === 1 && stats.traffic[0].source === "twelvedata", "traffic rows pass through");
  assert(stats.traffic[0].failed === 21, "failed requests are counted, not hidden behind landed rows");
  assert(stats.recentCalls.length === 2, "recent calls pass through");
  assert(stats.recentCalls[0].landed_fetch_id === 6540, "a successful call names the raw.fetches row it became");
  assert(
    stats.recentCalls[1].ok === false && stats.recentCalls[1].error !== null,
    "a failed call carries the vendor's error",
  );
  assert(stats.heads.length === 1, "one governed head");
  assert(stats.heads[0].availability === "0.7819", "capture-level headline rides along");
  assert(stats.heads[0].factors.length === 1, "factor grades unpacked from the payload map");
  assert(stats.heads[0].factors[0].factor_id === "gross_profit_per_employee", "factor id from the map key");
  assert(stats.heads[0].factors[0].ratio === "0.1275", "the honest number the 0.78 headline hid");
  assert(stats.sources[0].fetches_total === 728, "source stats pass through");
  assert(stats.runs[0].resolved === 408, "run stats pass through");
  assert(stats.validation.length === 4, "four validation checks, one row each");
  assert(stats.capacity.length === 4, "four capacity limits, one row each");
  assert(
    stats.capacity.every((row) => typeof row.verdict === "string" && typeof row.detail === "string"),
    "capacity rows carry the validation shape, not a mis-stubbed one",
  );
  assert(stats.validation[0].verdict === "pass", "validation verdicts pass through");
  __setTestOpsClient(null);
}

console.log("#641 admin datahub stats loader passed");
