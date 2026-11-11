/** Administrator-only INSERT into `staging.pipeline_trigger_requests` (#495, #874). */

import { randomUUID } from "node:crypto";
import type { PoolClient } from "pg";
import { UNIVERSES } from "@/contracts/environmentContract.generated";
import { withAppRuntime } from "@/server/auth/db";

type TriggerRuntime = <T>(fn: (client: Pick<PoolClient, "query">) => Promise<T>) => Promise<T>;

let runtimeOverride: TriggerRuntime | null = null;

export function __setTestTriggerRuntime(runtime: TriggerRuntime | null): void {
  runtimeOverride = runtime;
}

export interface TriggerRequestPrincipal {
  principalId: string;
  principalKind: "member" | "administrator" | "service";
}

export type TriggerJobName = `${string}_live_pipeline`;

const ALLOWED_JOBS = new Map<string, TriggerJobName>();
for (const universe of UNIVERSES) {
  const jobName: TriggerJobName = `${universe.key}_live_pipeline`;
  ALLOWED_JOBS.set(universe.key, jobName);
  ALLOWED_JOBS.set(jobName, jobName);
}

export type TriggerRequestOutcome =
  | {
      kind: "accepted";
      requestId: number;
      dedupeKey: string;
      executedAt: string;
      forceFetch: boolean;
      jobName: TriggerJobName;
    }
  | { kind: "denied" }
  | { kind: "invalid"; message: string }
  | { kind: "error"; message: string };

export async function requestPipelineTrigger(
  principal: TriggerRequestPrincipal | null,
  executedAtInput: string | undefined,
  forceFetchInput: unknown = undefined,
  jobNameInput: unknown = undefined,
): Promise<TriggerRequestOutcome> {
  if (principal === null || principal.principalKind !== "administrator") return { kind: "denied" };

  const executedAt = executedAtInput === undefined ? new Date() : new Date(executedAtInput);
  if (Number.isNaN(executedAt.getTime())) return { kind: "invalid", message: "executed_at must be an ISO 8601 timestamp" };
  if (forceFetchInput !== undefined && typeof forceFetchInput !== "boolean") {
    return { kind: "invalid", message: "force_fetch must be a boolean" };
  }
  const forceFetch = forceFetchInput === true;

  let jobName: TriggerJobName = "topt_live_pipeline";
  if (jobNameInput !== undefined) {
    const key = typeof jobNameInput === "string" ? ALLOWED_JOBS.get(jobNameInput.trim().toLowerCase()) : undefined;
    if (!key) {
      return { kind: "invalid", message: `job_name must be one of: ${UNIVERSES.map((u) => `${u.key}_live_pipeline`).join(", ")}` };
    }
    jobName = key;
  }

  const dedupeKey = `admin:${randomUUID()}`;
  const iso = executedAt.toISOString();
  const runtime: TriggerRuntime = runtimeOverride ?? withAppRuntime;
  try {
    const requestId = await runtime(async (client) => {
      const isTopt = jobName === "topt_live_pipeline";
      const cols = forceFetch
        ? "(job_name, executed_at, requested_by, dedupe_key, force_fetch)"
        : "(job_name, executed_at, requested_by, dedupe_key)";
      const vals = isTopt
        ? forceFetch
          ? "('topt_live_pipeline', $1, $2, $3, $4)"
          : "('topt_live_pipeline', $1, $2, $3)"
        : forceFetch
          ? "($1, $2, $3, $4, $5)"
          : "($1, $2, $3, $4)";
      const baseArgs = isTopt ? [iso, principal.principalId, dedupeKey] : [jobName, iso, principal.principalId, dedupeKey];
      const inserted = await client.query(
        `insert into staging.pipeline_trigger_requests ${cols} values ${vals} returning request_id`,
        forceFetch ? [...baseArgs, true] : baseArgs,
      );
      return Number(inserted.rows[0].request_id);
    });
    return { kind: "accepted", requestId, dedupeKey, executedAt: iso, forceFetch, jobName };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
