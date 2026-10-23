/**
 * #1466: the verdict table of /admin/datahub lists the CURRENT verdict of each check, one row per check, and
 * the history of one check is one query away. Against a real Postgres (skips without one; ci-web provisions
 * one and sets TRUEALPHA_REQUIRE_RUNTIME=1).
 *
 * `mart.nightly_verdicts` keeps every night and rejects UPDATE and DELETE. A bare `order by check_name`
 * listed the whole history: the same check once per night. The test appends several verdicts per check under
 * check names of its own and reads them through `app_ops_reader`, the role of the page.
 *
 * Run standalone: `CI=true DATABASE_URL=... bun run tests/admin-verdicts-current.test.ts`.
 */

import { Client } from "pg";

import { NIGHTLY_VERDICTS_SQL, NIGHTLY_VERDICT_HISTORY_SQL } from "../src/server/admin/datahub-stats";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const dbUrl = requireDb("admin-verdicts-current", { write: true });
if (!dbUrl) {
  process.exit(0);
}
process.env.DATABASE_URL = dbUrl;

// Check names of this run only: the table is append-only, so a re-run appends under a new suffix.
const SUFFIX = Date.now().toString(36);
const STEADY = `zz_current_verdict_steady_${SUFFIX}`;
const TIED = `zz_current_verdict_tied_${SUFFIX}`;

const client = new Client({ connectionString: dbUrl, connectionTimeoutMillis: 3000 });
try {
  await client.connect();
} catch (error) {
  await client.end().catch(() => {});
  if (process.env.TRUEALPHA_REQUIRE_RUNTIME) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
  console.log("admin-verdicts-current: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
  process.exit(0);
}

const insert = (check: string, ranAt: string, ok: boolean | null, summary: string, run: string) =>
  client.query(
    "insert into mart.nightly_verdicts (check_name, ran_at, ok, summary, dagster_run_id) values ($1, $2, $3, $4, $5)",
    [check, ranAt, ok, summary, run],
  );

try {
  // STEADY: three nights. The middle night is red and the newest night is green.
  await insert(STEADY, "2026-10-19T06:30:00Z", true, "night one", "run-1");
  await insert(STEADY, "2026-10-20T06:30:00Z", false, "night two", "run-2");
  await insert(STEADY, "2026-10-21T06:30:00Z", true, "night three", "run-3");
  // TIED: two rows for the same tick; the one recorded later wins, as on /api/health.
  await insert(TIED, "2026-10-21T06:30:00Z", false, "recorded first", "run-4");
  await insert(TIED, "2026-10-21T06:30:00Z", true, "recorded second", "run-5");

  await client.query("set role app_ops_reader");
  const current = await client.query(NIGHTLY_VERDICTS_SQL);
  const mine = current.rows.filter((row) => row.check_name === STEADY || row.check_name === TIED);

  assert(mine.length === 2, `one row per check, got ${mine.length} rows for 2 checks`);
  const names = current.rows.map((row) => row.check_name as string);
  assert(new Set(names).size === names.length, "no check name appears twice in the whole table");
  assert(
    JSON.stringify(names) === JSON.stringify([...names].sort()),
    "the rows stay ordered by check name",
  );
  const steady = mine.find((row) => row.check_name === STEADY);
  assert(steady?.summary === "night three" && steady.ok === true, "the current verdict is the newest night");
  assert(steady.ran_at === "2026-10-21 06:30:00Z", "the time is the tick of that night");
  const tied = mine.find((row) => row.check_name === TIED);
  assert(tied?.summary === "recorded second", "a tie on ran_at goes to the row recorded last");

  const history = await client.query(NIGHTLY_VERDICT_HISTORY_SQL, [STEADY]);
  assert(
    JSON.stringify(history.rows.map((row) => row.summary)) === JSON.stringify(["night three", "night two", "night one"]),
    "the history lists every night, newest first",
  );
  const other = await client.query(NIGHTLY_VERDICT_HISTORY_SQL, [`${STEADY}_none`]);
  assert(other.rows.length === 0, "the history of one check holds no row of another");
} finally {
  await client.query("reset role").catch(() => {});
  await client.end();
}
console.log("#1466 current verdict per check verified");
