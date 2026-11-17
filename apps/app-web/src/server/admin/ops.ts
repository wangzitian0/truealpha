/**
 * Administrator ops overview loader via `app_ops_reader`.
 */

import type { PoolClient } from "pg";
import { withRoleClient } from "../auth/db";
import {
  type HeadFreshness,
  REGISTERED_RAW_SOURCE_FILTER,
  UTC_DAY_START_SQL,
  resolveHeadFreshness,
} from "../../contracts/serviceObjectives";
import type { ReadState } from "@/server/read-state";

export type OpsQueryClient = Pick<PoolClient, "query">;
let testClientOverride: OpsQueryClient | null = null;

export function __setTestOpsClient(overrideClient: OpsQueryClient | null): void {
  testClientOverride = overrideClient;
}

export async function withOpsReader<T>(fn: (client: OpsQueryClient) => Promise<T>): Promise<T> {
  if (testClientOverride) {
    const injected = testClientOverride;
    await injected.query("set role app_ops_reader");
    try {
      return await fn(injected);
    } finally {
      await injected.query("reset role").catch(() => {});
    }
  }
  return withRoleClient("app_ops_reader", fn);
}

export const RUNS_UNAVAILABLE_MESSAGE: string =
  "Run history unavailable — Dagster has not bootstrapped its tables on this database.";
export const RUNS_EMPTY_MESSAGE: string =
  "No runs recorded yet — Dagster has its tables here but has not launched a run on this database.";
export const UNDEFINED_TABLE = "42P01";

export const errMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));
export const isUndefinedTable = (error: unknown): boolean =>
  typeof error === "object" && error !== null && (error as { code?: unknown }).code === UNDEFINED_TABLE;

export interface OpsPrincipal {
  principalKind: "member" | "administrator" | "service";
}

export async function runAdminRead<T>(
  principal: OpsPrincipal | null,
  load: () => Promise<ReadState<T>>,
  mapError?: (error: unknown) => ReadState<T> | null,
): Promise<ReadState<T>> {
  if (principal === null || principal.principalKind !== "administrator") return { kind: "denied" };
  try {
    return await load();
  } catch (error) {
    return mapError?.(error) ?? { kind: "error", message: errMessage(error) };
  }
}

export type OpsRunRow = { runId: string; jobName: string; status: string; createdAt: string; durationSeconds: number | null };
export type OpsPointerRow = {
  universeId: string; targetRunId: string; sequence: number; advancedAt: string;
  ageHours?: number | null; freshness?: HeadFreshness; availability?: "available" | "unavailable"; stalenessReason?: string | null;
};
export type OpsDataEngineBuild = { gitSha: string; imageDigest: string; runId: string; createdAt: string };
export type OpsOverview = {
  runs: OpsRunRow[] | "unavailable"; pointers: OpsPointerRow[]; dataEngine: OpsDataEngineBuild | null;
  appGitSha: string; quotaToday: { source: string; fetches: number }[];
};

export type OpsOverviewOutcome = ReadState<OpsOverview>;

export async function loadDataEngineBuild(client: OpsQueryClient): Promise<OpsDataEngineBuild | null> {
  let row: Record<string, unknown> | undefined;
  try {
    const result = await client.query(
      "select run_id, git_sha, image_digest, created_at from mart.data_engine_identity order by created_at desc limit 1",
    );
    row = result.rows[0];
  } catch (error) {
    if (isUndefinedTable(error)) return null;
    throw error;
  }
  return row === undefined
    ? null
    : {
        gitSha: String(row.git_sha),
        imageDigest: String(row.image_digest),
        runId: String(row.run_id),
        createdAt: new Date(row.created_at as string | number | Date).toISOString(),
      };
}

export const RELEASE_START_SQL = `
  select min(i.created_at) as since
  from mart.data_engine_identity i
  where i.git_sha = $1
    and i.created_at > coalesce(
      (select max(j.created_at) from mart.data_engine_identity j where j.git_sha <> $1 and j.git_sha <> 'unknown'),
      '-infinity'::timestamptz
    )
`;

export async function loadReleaseStart(client: OpsQueryClient, gitSha: string): Promise<string | null> {
  const result = await client.query(RELEASE_START_SQL, [gitSha]);
  const since = result.rows[0]?.since;
  return since == null ? null : new Date(since).toISOString();
}

export async function loadOpsOverview(principal: OpsPrincipal | null): Promise<OpsOverviewOutcome> {
  return runAdminRead(principal, async () => ({
    kind: "ready",
    data: await withOpsReader(async (client) => {
      let runs: OpsRunRow[] | "unavailable" = "unavailable";
      try {
        const result = await client.query(
          "select run_id, pipeline_name, status, create_timestamp, start_time, end_time from dagster.runs order by create_timestamp desc limit 10",
        );
        runs = result.rows.map((row) => ({
          runId: String(row.run_id),
          jobName: String(row.jobName ?? row.pipeline_name),
          status: String(row.status),
          createdAt: new Date(row.create_timestamp).toISOString(),
          durationSeconds:
            row.start_time !== null && row.end_time !== null ? Math.round(Number(row.end_time) - Number(row.start_time)) : null,
        }));
      } catch {
        // Dagster tables may not be bootstrapped yet.
      }

      const pointerResult = await client.query(
        "select environment, universe_id, coalesce(run_id, head_run_id) as target_run_id, sequence, " +
          "advanced_at, age_hours, freshness, availability, staleness_reason from mart.served_head order by universe_id",
      );
      const pointers: OpsPointerRow[] = pointerResult.rows.map((row) => {
        const universeId = String(row.universe_id);
        const env = row.environment != null ? String(row.environment) : null;
        return {
          universeId,
          targetRunId: String(row.target_run_id),
          sequence: Number(row.sequence),
          advancedAt: new Date(row.advanced_at).toISOString(),
          ageHours: row.age_hours != null ? Number(row.age_hours) : null,
          freshness: row.freshness ? resolveHeadFreshness(String(row.freshness), universeId, env) : undefined,
          availability: row.availability ? (String(row.availability) as "available" | "unavailable") : undefined,
          stalenessReason: row.staleness_reason ? String(row.staleness_reason) : null,
        };
      });

      const dataEngine = await loadDataEngineBuild(client).catch(() => null);
      const quotaResult = await client.query(
        `select source, count(*)::int as fetches from raw.fetches where fetched_at >= ${UTC_DAY_START_SQL} and ${REGISTERED_RAW_SOURCE_FILTER} group by source order by fetches desc`,
      );
      return {
        runs,
        pointers,
        dataEngine,
        appGitSha: process.env.GIT_COMMIT_SHA || "unknown",
        quotaToday: quotaResult.rows.map((row) => ({ source: String(row.source), fetches: Number(row.fetches) })),
      };
    }),
  }));
}
