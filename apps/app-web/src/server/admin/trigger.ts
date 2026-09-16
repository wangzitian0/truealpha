/**
 * #495 (surface 2): administrator-only INSERT into
 * `staging.pipeline_trigger_requests` — the app half of the DB-mediated
 * manual trigger (init.md §2.2: services exchange data only through
 * Postgres; the data-engine sensor consumes and launches, migration 0034).
 *
 * Lives under `src/server/admin/` — the naming convention the #493 boundary
 * test enforces: research routes and shared layers cannot import this.
 * Assumes `app_runtime` for the write so the grant shape stays exactly the
 * roles.sql exception (insert/select on this one staging table).
 *
 * #874: `force_fetch` asks the tick to skip the #635 reuse window and fetch
 * every obligation again, under a capture identity of its own. It must be a
 * boolean. Only a forced request names the column. An ordinary request keeps
 * the pre-#874 statement and takes the column's `false` default, so it still
 * works while a deploy waits for the migration (applied when llm-service boots).
 */

import { randomUUID } from "node:crypto";
import type { PoolClient } from "pg";
import { withAppRuntime } from "@/server/auth/db";

type TriggerRuntime = <T>(fn: (client: Pick<PoolClient, "query">) => Promise<T>) => Promise<T>;

let runtimeOverride: TriggerRuntime | null = null;

/** Test seam (same shape as `__setTestOpsClient`): replaces `withAppRuntime`. */
export function __setTestTriggerRuntime(runtime: TriggerRuntime | null): void {
  runtimeOverride = runtime;
}

export interface TriggerRequestPrincipal {
  principalId: string;
  principalKind: "member" | "administrator" | "service";
}

export type TriggerRequestOutcome =
  | { kind: "accepted"; requestId: number; dedupeKey: string; executedAt: string; forceFetch: boolean }
  | { kind: "denied" }
  | { kind: "invalid"; message: string }
  | { kind: "error"; message: string };

export async function requestPipelineTrigger(
  principal: TriggerRequestPrincipal | null,
  executedAtInput: string | undefined,
  forceFetchInput: unknown = undefined,
): Promise<TriggerRequestOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }

  const executedAt = executedAtInput === undefined ? new Date() : new Date(executedAtInput);
  if (Number.isNaN(executedAt.getTime())) {
    return { kind: "invalid", message: "executed_at must be an ISO 8601 timestamp" };
  }
  // Absent means an ordinary tick; anything but a real boolean is refused, so a
  // stray "false" string can never launch a forced run (or the reverse).
  if (forceFetchInput !== undefined && typeof forceFetchInput !== "boolean") {
    return { kind: "invalid", message: "force_fetch must be a boolean" };
  }
  const forceFetch = forceFetchInput === true;

  const dedupeKey = `admin:${randomUUID()}`;
  const runtime: TriggerRuntime = runtimeOverride ?? withAppRuntime;
  try {
    const requestId = await runtime(async (client) => {
      const inserted = forceFetch
        ? await client.query(
            "insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key, force_fetch) " +
              "values ('topt_live_pipeline', $1, $2, $3, $4) returning request_id",
            [executedAt.toISOString(), principal.principalId, dedupeKey, true],
          )
        : await client.query(
            "insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key) " +
              "values ('topt_live_pipeline', $1, $2, $3) returning request_id",
            [executedAt.toISOString(), principal.principalId, dedupeKey],
          );
      return Number(inserted.rows[0].request_id);
    });
    return { kind: "accepted", requestId, dedupeKey, executedAt: executedAt.toISOString(), forceFetch };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
