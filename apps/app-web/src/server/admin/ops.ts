/**
 * #495 (surface 2b): the /admin ops overview loader — administrator-only,
 * read-only SQL through the dedicated `app_ops_reader` role (db/roles.sql):
 * run history from `dagster.runs`, pointer freshness from
 * `mart.current_pointer_head`, per-source quota burn from `raw.fetches`.
 * Lives under `src/server/admin/` so the #493 boundary test keeps it
 * un-importable from research routes.
 *
 * Degrades honestly: on a database where Dagster has not bootstrapped its
 * own tables yet, the runs section reports `unavailable` instead of lying
 * with an empty list.
 */

import { Pool, type PoolClient } from "pg";

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

async function withOpsReader<T>(fn: (client: Pick<PoolClient, "query">) => Promise<T>): Promise<T> {
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

export interface OpsOverview {
  runs: OpsRunRow[] | "unavailable";
  pointer: { targetRunId: string; sequence: number; advancedAt: string } | null;
  quotaToday: { source: string; fetches: number }[];
}

export type OpsOverviewOutcome =
  | { kind: "ready"; data: OpsOverview }
  | { kind: "denied" }
  | { kind: "error"; message: string };

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
        "select target_run_id, sequence, advanced_at from mart.current_pointer_head " +
          "order by sequence desc limit 1",
      );
      const pointer =
        pointerResult.rows.length === 0
          ? null
          : {
              targetRunId: String(pointerResult.rows[0].target_run_id),
              sequence: Number(pointerResult.rows[0].sequence),
              advancedAt: new Date(pointerResult.rows[0].advanced_at).toISOString(),
            };

      const quotaResult = await client.query(
        "select source, count(*)::int as fetches from raw.fetches " +
          "where fetched_at >= date_trunc('day', now()) group by source order by fetches desc",
      );
      const quotaToday = quotaResult.rows.map((row) => ({
        source: String(row.source),
        fetches: Number(row.fetches),
      }));

      return { runs, pointer, quotaToday };
    });
    return { kind: "ready", data };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
