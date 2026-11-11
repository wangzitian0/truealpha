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

let testClientOverride: Pick<PoolClient, "query"> | null = null;

export function __setTestOpsClient(overrideClient: Pick<PoolClient, "query"> | null): void {
  testClientOverride = overrideClient;
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
  return withRoleClient("app_ops_reader", fn);
}

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

export interface OpsPointerRow {
  universeId: string;
  targetRunId: string;
  sequence: number;
  advancedAt: string;
  ageHours?: number | null;
  freshness?: HeadFreshness;
  availability?: "available" | "unavailable";
  stalenessReason?: string | null;
}

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
  appGitSha: string;
  quotaToday: { source: string; fetches: number }[];
}

export type OpsOverviewOutcome = ReadState<OpsOverview>;
export const UNDEFINED_TABLE = "42P01";

export async function loadDataEngineBuild(client: Pick<PoolClient, "query">): Promise<OpsDataEngineBuild | null> {
  let identityResult;
  try {
    identityResult = await client.query(
      "select run_id, git_sha, image_digest, created_at from mart.data_engine_identity order by created_at desc limit 1",
    );
  } catch (error) {
    if ((error as { code?: string }).code === UNDEFINED_TABLE) return null;
    throw error;
  }
  const row = identityResult.rows[0];
  return row === undefined
    ? null
    : {
        gitSha: String(row.git_sha),
        imageDigest: String(row.image_digest),
        runId: String(row.run_id),
        createdAt: new Date(row.created_at).toISOString(),
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

export async function loadReleaseStart(client: Pick<PoolClient, "query">, gitSha: string): Promise<string | null> {
  const result = await client.query(RELEASE_START_SQL, [gitSha]);
  const since = result.rows[0]?.since;
  return since === null || since === undefined ? null : new Date(since).toISOString();
}

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
          jobName: String(row.jobName ?? row.pipeline_name),
          status: String(row.status),
          createdAt: new Date(row.create_timestamp).toISOString(),
          durationSeconds:
            row.start_time !== null && row.end_time !== null
              ? Math.round(Number(row.end_time) - Number(row.start_time))
              : null,
        }));
      } catch {
        runs = "unavailable";
      }

      const pointerResult = await client.query(
        "select environment, universe_id, coalesce(run_id, head_run_id) as target_run_id, sequence, " +
          "advanced_at, age_hours, freshness, availability, staleness_reason " +
          "from mart.served_head order by universe_id",
      );
      const pointers: OpsPointerRow[] = pointerResult.rows.map((row) => {
        const universeId = String(row.universe_id);
        const environment = row.environment !== null && row.environment !== undefined ? String(row.environment) : null;
        return {
          universeId,
          targetRunId: String(row.target_run_id),
          sequence: Number(row.sequence),
          advancedAt: new Date(row.advanced_at).toISOString(),
          ageHours: row.age_hours !== null && row.age_hours !== undefined ? Number(row.age_hours) : null,
          freshness: row.freshness ? resolveHeadFreshness(String(row.freshness), universeId, environment) : undefined,
          availability: row.availability ? (String(row.availability) as "available" | "unavailable") : undefined,
          stalenessReason: row.staleness_reason ? String(row.staleness_reason) : null,
        };
      });

      const dataEngine = await loadDataEngineBuild(client).catch(() => null);
      const quotaResult = await client.query(
        "select source, count(*)::int as fetches from raw.fetches " +
          `where fetched_at >= ${UTC_DAY_START_SQL} and ${REGISTERED_RAW_SOURCE_FILTER} group by source order by fetches desc`,
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
