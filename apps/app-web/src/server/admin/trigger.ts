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
 */

import { randomUUID } from "node:crypto";
import { withAppRuntime } from "@/server/auth/db";

export interface TriggerRequestPrincipal {
  principalId: string;
  principalKind: "member" | "administrator" | "service";
}

export type TriggerRequestOutcome =
  | { kind: "accepted"; requestId: number; dedupeKey: string; executedAt: string }
  | { kind: "denied" }
  | { kind: "invalid"; message: string }
  | { kind: "error"; message: string };

export async function requestPipelineTrigger(
  principal: TriggerRequestPrincipal | null,
  executedAtInput: string | undefined,
): Promise<TriggerRequestOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }

  const executedAt = executedAtInput === undefined ? new Date() : new Date(executedAtInput);
  if (Number.isNaN(executedAt.getTime())) {
    return { kind: "invalid", message: "executed_at must be an ISO 8601 timestamp" };
  }

  const dedupeKey = `admin:${randomUUID()}`;
  try {
    const requestId = await withAppRuntime(async (client) => {
      const inserted = await client.query(
        "insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key) " +
          "values ('topt_live_pipeline', $1, $2, $3) returning request_id",
        [executedAt.toISOString(), principal.principalId, dedupeKey],
      );
      return Number(inserted.rows[0].request_id);
    });
    return { kind: "accepted", requestId, dedupeKey, executedAt: executedAt.toISOString() };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
