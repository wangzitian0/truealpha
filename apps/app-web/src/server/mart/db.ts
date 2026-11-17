import type { PoolClient } from "pg";
import { withRoleClient } from "../auth/db";

let testClientOverride: Pick<PoolClient, "query"> | null = null;

export interface QueryClientLike {
  query(sql: string, params?: readonly unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

export const POINTER_HEAD_SQL = `
  select run_id, freshness, availability, staleness_reason
  from mart.served_head
  where factor_id = 'gross_profit_per_employee'
    and universe_id like $1
  order by advanced_at desc limit 1
`;

export const ACCEPTANCE_FALLBACK_HEAD_SQL = `
  select s.run_id
  from mart.topt_capture_status s
  join mart.datahub_quality_report q on q.run_id = s.run_id
  where s.environment = (select environment from mart.environment_identity)
    and s.complete
    and s.universe_id like $1
  order by q.created_at desc, q.report_id desc limit 1
`;

export function servedRunPredicate(column: string): string {
  return `${column} in (select h.run_id from mart.served_head h where h.run_id is not null)`;
}

export async function resolveGovernedHeadRunId(
  client: QueryClientLike,
  universeLike: string,
): Promise<{ runId: unknown; reason: string | null }> {
  let row = (await client.query(POINTER_HEAD_SQL, [universeLike])).rows[0];
  if (row !== undefined) {
    if (row.availability === "unavailable" || row.run_id === null) {
      const stale =
        typeof row.staleness_reason === "string" && row.staleness_reason.length > 0
          ? row.staleness_reason
          : "older_than_30d";
      return { runId: null, reason: `head_unavailable: ${stale}` };
    }
  } else {
    row = (await client.query(ACCEPTANCE_FALLBACK_HEAD_SQL, [universeLike])).rows[0];
    if (row === undefined || row.run_id === null) {
      return { runId: null, reason: "no accepted (quality-reported) production TOPT run" };
    }
  }
  return { runId: row.run_id, reason: null };
}

export function __setTestClient(overrideClient: Pick<PoolClient, "query"> | null): void {
  testClientOverride = overrideClient;
}

export async function withMartReadonly<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  if (testClientOverride) {
    const injected = testClientOverride as PoolClient;
    try {
      await injected.query("set role mart_readonly");
      return await fn(injected);
    } finally {
      await injected.query("reset role").catch(() => {});
    }
  }
  return withRoleClient("mart_readonly", fn);
}

export async function queryMartRows<T>(sql: string, params?: readonly unknown[]): Promise<T[]> {
  return withMartReadonly(async (client) => {
    const result = await client.query(sql, params as unknown[]);
    return result.rows as T[];
  });
}
