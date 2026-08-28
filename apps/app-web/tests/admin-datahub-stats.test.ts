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
      if (typeof sql === "string" && sql.includes("raw.fetches")) return { rows: sourceRows } as never;
      return { rows: runRows } as never;
    },
  } as never);
  const stats = await loadDatahubStats();
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
