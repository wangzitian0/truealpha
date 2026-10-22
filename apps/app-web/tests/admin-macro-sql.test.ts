/**
 * #1255: the two statements of the macro panel loader against a real Postgres.
 *
 * `tests/admin-macro.test.ts` answers the statements with a fake. A fake cannot show that a column
 * exists, that the window of 12 months is right, or that the order is right. This test seeds three
 * series in the real tables and runs the real `loadMacroPanel` through `mart_readonly`:
 *   - `t1255_fresh`: four dates in the window and one before it; the newest date has two vintages;
 *   - `t1255_stale`: a date 9 days old (the limit is 5 days) and a date at the oldest edge of the window;
 *   - `t1255_empty`: a catalog row without an observation.
 * The test checks these properties:
 *   1. The status row of each series carries its catalog fields, its newest observation, its newest
 *      vintage (value and fetch time) and the verdict of the view. A series without a row has no date.
 *   2. The months hold the newest vintage of each date only, the date at the oldest edge of the window,
 *      and no date before the window.
 *   3. The months are newest date first, with the series as the tie-break. The order is by date across
 *      series, not by series.
 *   4. The date reads `YYYY-MM-DD` under a server `DateStyle` that is not ISO.
 *   5. The loader reads through `mart_readonly`: the role has `select` on both views.
 * Only the rows of the three test series are asserted, so another row in the catalog cannot change the verdict.
 *
 * Throws when the database is unreachable (ci-web sets DATABASE_URL and TRUEALPHA_REQUIRE_RUNTIME=1).
 * All rows live in ONE transaction on the test's own client, lent to the loader through db.ts's
 * `__setTestClient`. The transaction is rolled back.
 * Run standalone: `CI=true DATABASE_URL=... bun run tests/admin-macro-sql.test.ts`.
 */

import { randomUUID } from "node:crypto";

import { Client, type PoolClient } from "pg";

import { loadMacroPanel, MONTHS_SQL, STATUS_SQL } from "../src/server/admin/macro";
import { __setTestClient, withMartReadonly } from "../src/server/mart/db";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const DATABASE_URL = requireDb("admin-macro-sql");
if (!DATABASE_URL) {
  process.exit(0);
}
process.env.DATABASE_URL = DATABASE_URL;

const EMPTY = "t1255_empty";
const FRESH = "t1255_fresh";
const STALE = "t1255_stale";
const SEEDED = [EMPTY, FRESH, STALE]; // the order of `order by series_id`
const UNIT = "annualized_decimal_fraction";

/** The UTC date of the database: the seed and the views agree on "today". */
const TODAY_SQL = "(now() at time zone 'UTC')::date";
/** The oldest day of the 12-month window, by the rule of the loader. */
const EDGE_SQL = `(${TODAY_SQL} - interval '12 months')::date`;

const admin = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
await admin.connect();

/** A day of the database as `YYYY-MM-DD`: `daysAgo` days before today. */
async function dayAgo(daysAgo: number): Promise<string> {
  const result = await admin.query(`select to_char(${TODAY_SQL} - $1::int, 'YYYY-MM-DD') as d`, [daysAgo]);
  return String(result.rows[0].d);
}

interface StoredFetch {
  id: number;
  /** The fetch time as the loader returns it. The seed uses whole seconds, so the milliseconds are zero. */
  fetchedAt: string;
}

/** One stored fetch. The trigger of `mart.macro_series_daily` demands a row of `raw.fetches`. */
async function storeFetch(daysAgo: number): Promise<StoredFetch> {
  const result = await admin.query(
    `insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, byte_length, fetched_at)
     values ('us-treasury', $1, $2, 's3://test-1255/par-yield.csv', 'text/csv', 10,
             date_trunc('second', now()) - ($3::int * interval '1 day'))
     returning id, to_char(fetched_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS".000Z"') as fetched_at`,
    [`test:1255:${randomUUID()}`, "a".repeat(64), daysAgo],
  );
  return { id: Number(result.rows[0].id), fetchedAt: String(result.rows[0].fetched_at) };
}

/** One observation. `recordedDaysAgo` orders the vintages of one date: the larger value is the older row. */
async function addObservation(
  seriesId: string,
  observationDaysAgo: number,
  value: string,
  fetch: StoredFetch,
  fetchedDaysAgo: number,
  recordedDaysAgo: number,
): Promise<void> {
  await admin.query(
    `insert into mart.macro_series_daily
       (series_id, observation_date, value, knowable_at, source, fetch_id, fetched_at, confidence, recorded_at)
     values ($1, ${TODAY_SQL} - $2::int, $3::numeric, (${TODAY_SQL} - $2::int + 1)::timestamp at time zone 'UTC',
             'us-treasury', $4, date_trunc('second', now()) - ($5::int * interval '1 day'), 1,
             now() - ($6::int * interval '1 day'))`,
    [seriesId, observationDaysAgo, value, fetch.id, fetchedDaysAgo, recordedDaysAgo],
  );
}

try {
  await admin.query("begin");
  __setTestClient(admin as unknown as Pick<PoolClient, "query">);

  for (const seriesId of SEEDED) {
    await admin.query(
      `insert into mart.macro_series (series_id, label, unit, source_id, max_age_days)
       values ($1, $2, $3, 'us-treasury-par-yield', 5)`,
      [seriesId, `Label of ${seriesId}`, UNIT],
    );
  }

  // The window: `EDGE` days ago is its oldest day. One day before is outside.
  const edge: number = (await admin.query(`select ${TODAY_SQL} - ${EDGE_SQL} as days`)).rows[0].days;
  assert(edge >= 365 && edge <= 366, `the window is a year of 365 or 366 days: ${edge}`);

  const originalFetch = await storeFetch(3);
  const revisedFetch = await storeFetch(1);
  const staleFetch = await storeFetch(9);

  // Inserted oldest first, so a statement without an `order by` cannot return the dates newest first by accident.
  await addObservation(FRESH, edge + 1, "0.0100", originalFetch, 3, 30); // the day before the window
  await addObservation(FRESH, edge, "0.0200", originalFetch, 3, 29); // the oldest day of the window
  await addObservation(STALE, edge, "0.0250", staleFetch, 9, 29); // the same date in another series
  await addObservation(STALE, 9, "0.0479", staleFetch, 9, 9); // the newest date of the stale series
  await addObservation(FRESH, 3, "0.0300", originalFetch, 3, 3);
  await addObservation(FRESH, 1, "0.0400", originalFetch, 3, 2); // the original value of the newest date
  await addObservation(FRESH, 1, "0.0411", revisedFetch, 1, 1); // the revised value, recorded later

  const outcome = await loadMacroPanel({ principalKind: "administrator" });
  assert(outcome.kind === "ready", `the loader must be ready through mart_readonly: ${JSON.stringify(outcome)}`);

  // 1. The status rows: catalog fields, the newest observation, its newest vintage, the verdict of the view.
  const series = outcome.data.series.filter((item) => SEEDED.includes(item.seriesId));
  assert(
    series.map((item) => item.seriesId).join() === SEEDED.join(),
    `the series come in the order of series_id: ${series.map((item) => item.seriesId)}`,
  );
  const [empty, fresh, stale] = series;
  for (const item of series) {
    assert(item.label === `Label of ${item.seriesId}` && item.unit === UNIT, `catalog fields of ${item.seriesId}`);
    assert(item.sourceId === "us-treasury-par-yield" && item.maxAgeDays === 5, `source and limit of ${item.seriesId}`);
  }
  assert(empty.latest === null && empty.isFresh === false, `a series without a row: ${JSON.stringify(empty)}`);
  assert(
    JSON.stringify(fresh.latest) ===
      JSON.stringify({
        observationDate: await dayAgo(1),
        value: "0.0411",
        source: "us-treasury",
        fetchedAt: revisedFetch.fetchedAt,
        ageDays: 1,
      }),
    `the newest vintage of the newest date: ${JSON.stringify(fresh.latest)}`,
  );
  assert(fresh.isFresh === true, "an observation of 1 day against a limit of 5 days is fresh");
  assert(
    JSON.stringify(stale.latest) ===
      JSON.stringify({
        observationDate: await dayAgo(9),
        value: "0.0479",
        source: "us-treasury",
        fetchedAt: staleFetch.fetchedAt,
        ageDays: 9,
      }),
    `the newest observation of the stale series: ${JSON.stringify(stale.latest)}`,
  );
  assert(stale.isFresh === false, "an observation of 9 days against a limit of 5 days is stale");

  // 2 and 3. The months: newest vintage only, the window, newest date first, the series as the tie-break.
  const months = outcome.data.months.filter((row) => SEEDED.includes(row.seriesId));
  const expected = [
    [FRESH, await dayAgo(1), "0.0411"], // not 0.0400: the older vintage of the date is not served
    [FRESH, await dayAgo(3), "0.0300"],
    [STALE, await dayAgo(9), "0.0479"], // between two dates of the fresh series: the order is by date, not by series
    [FRESH, await dayAgo(edge), "0.0200"], // the oldest day of the window is inside
    [STALE, await dayAgo(edge), "0.0250"], // the tie of the date goes to the smaller series id
  ];
  const got = months.map((row) => [row.seriesId, row.observationDate, row.value]);
  assert(
    JSON.stringify(got) === JSON.stringify(expected),
    `months:\n got      ${JSON.stringify(got)}\n expected ${JSON.stringify(expected)}`,
  );

  // 4. The date text does not depend on the server DateStyle. `date::text` would read `22.10.2026` here.
  await admin.query("set local datestyle = 'German, DMY'");
  const dayTexts = await withMartReadonly(async (client) => {
    const status = await client.query(STATUS_SQL);
    const monthRows = await client.query(MONTHS_SQL);
    return [...status.rows, ...monthRows.rows]
      .filter((row) => SEEDED.includes(String(row.series_id)))
      .map((row) => row.observation_day as string | null);
  });
  assert(dayTexts.length === 3 + 5, `status rows (3) and month rows (5) of the seeded series: ${dayTexts.length}`);
  for (const day of dayTexts) {
    assert(day === null || /^\d{4}-\d{2}-\d{2}$/.test(day), `a date reads YYYY-MM-DD under any DateStyle: ${day}`);
  }
  await admin.query("set local datestyle = 'ISO, MDY'");

  console.log("admin-macro-sql: ok");
} finally {
  __setTestClient(null);
  await admin.query("rollback").catch(() => {});
  await admin.end();
}
