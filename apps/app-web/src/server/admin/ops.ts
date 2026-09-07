/**
 * #495 (surface 2b): the /admin ops overview loader — administrator-only,
 * read-only SQL through the dedicated `app_ops_reader` role (db/roles.sql):
 * run history from `dagster.runs`, pointer freshness per universe from
 * `mart.current_pointer_head`, the data-engine build behind the newest run from
 * `mart.data_engine_identity` (#712), per-source quota burn from `raw.fetches`.
 * Lives under `src/server/admin/` so the #493 boundary test keeps it
 * un-importable from research routes.
 *
 * Degrades honestly: on a database where Dagster has not bootstrapped its
 * own tables yet, the runs section reports `unavailable` instead of lying
 * with an empty list.
 */

import { Pool, type PoolClient } from "pg";
import type { ReadState } from "@/server/read-state";

let pool: Pool | null = null;
let testClientOverride: Pick<PoolClient, "query"> | null = null;

export function __setTestOpsClient(overrideClient: Pick<PoolClient, "query"> | null): void {
  testClientOverride = overrideClient;
}

function getPool(): Pool {
  if (!pool) {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
      throw new Error("DATABASE_URL is not set");
    }
    pool = new Pool({ connectionString });
  }
  return pool;
}

export async function withOpsReader<T>(fn: (client: Pick<PoolClient, "query">) => Promise<T>): Promise<T> {
  if (testClientOverride) {
    const injected = testClientOverride;
    await injected.query("set role app_ops_reader");
    try {
      return await fn(injected);
    } finally {
      await injected.query("reset role").catch(() => {});
    }
  }
  const client = await getPool().connect();
  try {
    await client.query("set role app_ops_reader");
    return await fn(client);
  } finally {
    await client.query("reset role").catch(() => {});
    client.release();
  }
}

/** #495: the run table's two absent states, owned by the loader that produces
 * them so a test can assert they stay different sentences. "Dagster has no
 * tables here" and "Dagster has tables but no runs" are different operational
 * facts; the page used to render the second one as a bare header. */
export const RUNS_UNAVAILABLE_MESSAGE: string =
  "Run history unavailable — Dagster has not bootstrapped its tables on this database.";
export const RUNS_EMPTY_MESSAGE: string =
  "No runs recorded yet — Dagster has its tables here but has not launched a run on this database.";

export interface OpsPrincipal {
  principalKind: "member" | "administrator" | "service";
}

export interface OpsRunRow {
  runId: string;
  jobName: string;
  status: string;
  createdAt: string;
  durationSeconds: number | null;
}

/** One governed head per universe. The page renders every row: the ops question is
 * "did EACH pipeline advance", and a single collapsed row answered it for whichever
 * universe happened to sort first (the canary, alphabetically) while the core stalled. */
export interface OpsPointerRow {
  universeId: string;
  targetRunId: string;
  sequence: number;
  advancedAt: string;
}

/** #712: which data-engine build produced the newest run, from
 * `mart.data_engine_identity`. `null` when the view has no rows (no run recorded
 * yet) or when the database predates the view (the 2026-09-07 migration); a run
 * recorded before the identity was stamped reads "unknown"/"unknown", not null. */
export interface OpsDataEngineBuild {
  gitSha: string;
  imageDigest: string;
  runId: string;
  createdAt: string;
}

export interface OpsOverview {
  runs: OpsRunRow[] | "unavailable";
  pointers: OpsPointerRow[];
  dataEngine: OpsDataEngineBuild | null;
  /** The app's own build, from the deployer-set GIT_COMMIT_SHA; "unknown" when unset. */
  appGitSha: string;
  quotaToday: { source: string; fetches: number }[];
}

/** #495: the SAME `ReadState` union the research surfaces use, so the shared
 * `ReadStateNotice` renders this page's absence states and every one of them
 * is proven to have words by `tests/read-state.test.ts`. The three cases this
 * loader can actually produce are `ready`, `denied` and `error`; the union is
 * wider because it is shared, and the renderer covers the rest. */
export type OpsOverviewOutcome = ReadState<OpsOverview>;

export async function loadOpsOverview(principal: OpsPrincipal | null): Promise<OpsOverviewOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }
  try {
    const data = await withOpsReader(async (client) => {
      let runs: OpsRunRow[] | "unavailable";
      try {
        const result = await client.query(
          "select run_id, pipeline_name, status, create_timestamp, start_time, end_time " +
            "from dagster.runs order by create_timestamp desc limit 10",
        );
        runs = result.rows.map((row) => ({
          runId: String(row.run_id),
          jobName: String(row.pipeline_name),
          status: String(row.status),
          createdAt: new Date(row.create_timestamp).toISOString(),
          durationSeconds:
            row.start_time !== null && row.end_time !== null
              ? Math.round(Number(row.end_time) - Number(row.start_time))
              : null,
        }));
      } catch {
        // Dagster bootstraps its own tables at runtime; a database without
        // them (fresh local/CI) reports the section as unavailable.
        runs = "unavailable";
      }

      const pointerResult = await client.query(
        // An ops overview reports pointer freshness PER UNIVERSE. Collapsing with
        // `order by sequence desc limit 1` showed one universe and hid the rest, so a
        // canary that stopped advancing looked identical to one that never ran.
        "select universe_id, target_run_id, sequence, advanced_at from mart.current_pointer_head " +
          "order by universe_id",
      );
      const pointers: OpsPointerRow[] = pointerResult.rows.map((row) => ({
        universeId: String(row.universe_id),
        targetRunId: String(row.target_run_id),
        sequence: Number(row.sequence),
        advancedAt: new Date(row.advanced_at).toISOString(),
      }));

      let dataEngine: OpsDataEngineBuild | null = null;
      try {
        const identityResult = await client.query(
          "select run_id, git_sha, image_digest, created_at from mart.data_engine_identity " +
            "order by created_at desc limit 1",
        );
        const row = identityResult.rows[0];
        dataEngine =
          row === undefined
            ? null
            : {
                gitSha: String(row.git_sha),
                imageDigest: String(row.image_digest),
                runId: String(row.run_id),
                createdAt: new Date(row.created_at).toISOString(),
              };
      } catch {
        // The view arrives with the 2026-09-07 migration; a database that predates it
        // reports the build as unknown rather than failing the whole overview.
        dataEngine = null;
      }

      const quotaResult = await client.query(
        "select source, count(*)::int as fetches from raw.fetches " +
          "where fetched_at >= date_trunc('day', now()) group by source order by fetches desc",
      );
      const quotaToday = quotaResult.rows.map((row) => ({
        source: String(row.source),
        fetches: Number(row.fetches),
      }));

      return { runs, pointers, dataEngine, appGitSha: process.env.GIT_COMMIT_SHA || "unknown", quotaToday };
    });
    return { kind: "ready", data };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
