/**
 * #1521 step 6: the key and run coverage table of /admin/datahub, against a real Postgres.
 *
 * The table replaces the nightly verdict `fetch_keys_once`. `tests/admin-datahub-stats.test.ts` answers the
 * statements with a fake. A fake cannot show that a column exists or that a count is right. This test seeds rows
 * of one test vendor in `staging.api_call_ledger` and runs the real `loadDatahubStats` as `app_ops_reader`.
 *
 * Rows of endpoint `t1521_ep_a`, today (UTC), ten rows:
 *   - key K1 twice, ok, with a run                   -> one key fetched twice
 *   - key K2 ok, then a failed retry of K2            -> a failed retry is no duplicate
 *   - key K3 twice, ok, reason `probe`, no run        -> a repeat with a reason is no duplicate
 *   - an ok row with no key, with a run               -> one row without a key
 *   - key K4 once, ok, reason `answer_refused`        -> second reason
 *   - a failed row with no key and no run             -> failed rows count as calls and as rows without a key
 *   - a failed row with key K9, reason `probe`        -> failed rows count per reason
 * Endpoint `t1521_ep_a`, yesterday: K2 once more, and one other key, no run. K2 has two successful rows over two
 * UTC days: the same key on another UTC day is no duplicate.
 * Endpoint `t1521_ep_b`, today: one ok row with a key and a run.
 * Two edge rows: the oldest day of the window (read) and the day before it (not read).
 *
 * The test checks these properties:
 *   1. Each column holds the expected count for each (day, endpoint): calls, no_key, by_reason, keys_twice.
 *   2. SQL computes the run share and the calls per run. A day with no run has a share of 0 and no calls per run.
 *   3. A day is a UTC day. The loader runs in a session whose local date is not the UTC date.
 *   4. The window holds LEDGER_COVERAGE_DAYS days. The day before the oldest is not read.
 *   5. `app_ops_reader` reads every column. The loader sets that role.
 * Only the rows of the test vendor are asserted, so another ledger row cannot change the verdict.
 *
 * Throws when the database is unreachable (ci-web sets DATABASE_URL and TRUEALPHA_REQUIRE_RUNTIME=1).
 * All rows live in ONE transaction on the test's own client, lent to the loader through ops.ts's
 * `__setTestOpsClient`. The transaction is rolled back.
 * Run standalone: `CI=true DATABASE_URL=... bun run tests/admin-ledger-coverage-sql.test.ts`.
 */

import { Client, type PoolClient } from "pg";

import { LEDGER_COVERAGE_DAYS, loadDatahubStats, type LedgerCoverageRow } from "../src/server/admin/datahub-stats";
import { __setTestOpsClient } from "../src/server/admin/ops";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const DATABASE_URL = requireDb("admin-ledger-coverage-sql");
if (!DATABASE_URL) {
  process.exit(0);
}
process.env.DATABASE_URL = DATABASE_URL;

const SOURCE = "t1521_vendor";
const EP_A = "t1521_ep_a";
const EP_B = "t1521_ep_b";

const admin = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
await admin.connect();

/** The UTC date of the database as `YYYY-MM-DD`, `daysAgo` days before today. */
async function dayAgo(daysAgo: number): Promise<string> {
  const result = await admin.query(
    "select to_char((now() at time zone 'utc')::date - $1::int, 'YYYY-MM-DD') as d",
    [daysAgo],
  );
  return String(result.rows[0].d);
}

interface Seed {
  endpoint: string;
  /** Minutes after 00:00 UTC today; negative values fall on an earlier day. */
  minutes: number;
  ok?: boolean;
  /** The entity part of the fetch key; null seeds a row with no key. */
  entity: string | null;
  run: string | null;
  reason?: string;
}

async function seed(row: Seed): Promise<void> {
  const fetched = row.entity === null ? null : `${SOURCE}|${row.endpoint}|${row.entity}|2026-10-27`;
  await admin.query(
    `insert into staging.api_call_ledger (source, endpoint, caller, called_at, ok, cost, fetch_key, run_key, repeat_reason)
     values ($1, $2, 't1521-test',
             (date_trunc('day', now() at time zone 'utc') at time zone 'utc') + $3::int * interval '1 minute',
             $4, 1, $5, $6, $7)`,
    [SOURCE, row.endpoint, row.minutes, row.ok ?? true, fetched, row.run, row.reason ?? null],
  );
}

function ours(rows: LedgerCoverageRow[]): LedgerCoverageRow[] {
  return rows.filter((row) => row.source === SOURCE);
}

function find(rows: LedgerCoverageRow[], day: string, endpoint: string): LedgerCoverageRow {
  const found = rows.filter((row) => row.day === day && row.endpoint === endpoint);
  assert(found.length === 1, `exactly one row for ${day} ${endpoint}, got ${found.length}`);
  return found[0];
}

try {
  await admin.query("begin");
  __setTestOpsClient(admin as unknown as Pick<PoolClient, "query">);
  // 3. A session far from UTC: Kiritimati is UTC+14, so its local date is never the UTC date for part of the day.
  await admin.query("set local time zone 'Pacific/Kiritimati'");

  const today = await dayAgo(0);
  const yesterday = await dayAgo(1);
  const oldest = await dayAgo(LEDGER_COVERAGE_DAYS - 1);
  const beforeOldest = await dayAgo(LEDGER_COVERAGE_DAYS);

  // Today, endpoint A: eight rows.
  await seed({ endpoint: EP_A, minutes: 1, entity: "symbol:k1", run: "dagster:r1" });
  await seed({ endpoint: EP_A, minutes: 2, entity: "symbol:k1", run: "dagster:r1" });
  await seed({ endpoint: EP_A, minutes: 3, entity: "symbol:k2", run: "dagster:r2" });
  await seed({ endpoint: EP_A, minutes: 4, entity: "symbol:k2", run: "dagster:r2", ok: false });
  await seed({ endpoint: EP_A, minutes: 5, entity: "symbol:k3", run: null, reason: "probe" });
  await seed({ endpoint: EP_A, minutes: 6, entity: "symbol:k3", run: null, reason: "probe" });
  await seed({ endpoint: EP_A, minutes: 7, entity: null, run: "dagster:r2" });
  await seed({ endpoint: EP_A, minutes: 8, entity: "symbol:k4", run: "dagster:r3", reason: "answer_refused" });
  await seed({ endpoint: EP_A, minutes: 9, entity: null, run: null, ok: false });
  await seed({ endpoint: EP_A, minutes: 10, entity: "symbol:k9", run: null, ok: false, reason: "probe" });
  // Yesterday, endpoint A: K2 again and one other key, no run. Two rows, no key twice on that day.
  await seed({ endpoint: EP_A, minutes: -1, entity: "symbol:k2", run: null });
  await seed({ endpoint: EP_A, minutes: -2, entity: "symbol:k8", run: null });
  // Today, endpoint B: one row.
  await seed({ endpoint: EP_B, minutes: 11, entity: "symbol:k5", run: "dagster:r4" });
  // The edges of the window: the first minute of the oldest day is read, the last minute before it is not.
  await seed({ endpoint: EP_B, minutes: -60 * 24 * (LEDGER_COVERAGE_DAYS - 1), entity: "symbol:k6", run: null });
  await seed({ endpoint: EP_B, minutes: -60 * 24 * (LEDGER_COVERAGE_DAYS - 1) - 1, entity: "symbol:k7", run: null });

  const stats = await loadDatahubStats();
  const rows = ours(stats.ledgerCoverage);

  // 1 and 2. Today, endpoint A.
  const a = find(rows, today, EP_A);
  assert(a.calls === 10, `today A: 10 calls, got ${a.calls}`);
  assert(a.no_key === 2, `today A: 2 rows without a key, got ${a.no_key}`);
  assert(
    JSON.stringify(Object.entries(a.by_reason).sort()) ===
      JSON.stringify([
        ["answer_refused", 1],
        ["probe", 3],
      ]),
    `today A: rows per reason, got ${JSON.stringify(a.by_reason)}`,
  );
  assert(a.keys_twice === 1, `today A: only K1 is fetched twice, got ${a.keys_twice}`);
  assert(a.run_share === "0.6000", `today A: 6 of 10 rows name a run, got ${a.run_share}`);
  assert(a.calls_per_run === "2.0", `today A: 6 rows over 3 runs, got ${a.calls_per_run}`);

  // Yesterday, endpoint A: K2 on another UTC day is no duplicate, and no row names a run.
  const y = find(rows, yesterday, EP_A);
  assert(y.calls === 2 && y.no_key === 0, `yesterday A: 2 calls and 0 without a key, got ${JSON.stringify(y)}`);
  assert(y.keys_twice === 0, `yesterday A: no key is fetched twice on that day, got ${y.keys_twice}`);
  assert(Object.keys(y.by_reason).length === 0, `yesterday A: no reason, got ${JSON.stringify(y.by_reason)}`);
  assert(y.run_share === "0.0000", `yesterday A: no row names a run, got ${y.run_share}`);
  assert(y.calls_per_run === null, `yesterday A: no run, so no calls per run, got ${y.calls_per_run}`);

  const b = find(rows, today, EP_B);
  assert(b.calls === 1 && b.no_key === 0 && b.keys_twice === 0, `today B: ${JSON.stringify(b)}`);
  assert(b.run_share === "1.0000" && b.calls_per_run === "1.0", `today B: one row, one run, got ${JSON.stringify(b)}`);

  // 3. Rows at 23:58 and 00:01 UTC share one local date in Kiritimati (UTC+14); the day column must still split them.
  assert(a.calls === 10 && y.calls === 2, "a row at 00:01 UTC is of today and a row at 23:58 UTC is of yesterday");

  // 4. The window.
  const days = new Set(rows.map((row) => row.day));
  assert(days.has(oldest), `the oldest day of the window is read: ${oldest}`);
  assert(!days.has(beforeOldest), `the day before the window is not read: ${beforeOldest}`);
  assert(find(rows, oldest, EP_B).calls === 1, "the first minute of the oldest day is read");

  // The order: newest day first.
  const order = rows.map((row) => `${row.day} ${row.endpoint}`);
  assert(
    order.join("|") ===
      [`${today} ${EP_A}`, `${today} ${EP_B}`, `${yesterday} ${EP_A}`, `${oldest} ${EP_B}`].join("|"),
    `rows are newest day first, then endpoint: ${order.join(" | ")}`,
  );

  console.log("admin-ledger-coverage-sql: ok");
} finally {
  __setTestOpsClient(null);
  await admin.query("rollback").catch(() => {});
  await admin.end();
}
