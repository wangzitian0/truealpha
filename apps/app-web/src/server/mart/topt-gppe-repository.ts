/**
 * TypeScript mirror of `truealpha_contracts.topt_read.PostgresToptGppeRepository`.
 */

import type { ToptGppeCell, ToptGppeReport, ToptGppeUnavailable } from "@/contracts/toptGppe";
import { idPrefixLikeOfRole } from "@/contracts/universes";
import { withMartReadonly } from "./db";

export interface MartClientLike {
  query(sql: string, params?: readonly unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

const POINTER_HEAD_SQL = `
  select run_id, freshness, availability, staleness_reason
  from mart.served_head
  where factor_id = 'gross_profit_per_employee'
    and universe_id like $1
  order by advanced_at desc limit 1
`;

const ACCEPTANCE_FALLBACK_HEAD_SQL = `
  select s.run_id
  from mart.topt_capture_status s
  join mart.datahub_quality_report q on q.run_id = s.run_id
  where s.environment = (select environment from mart.environment_identity)
    and s.complete
    and s.universe_id like $1
  order by q.created_at desc, q.report_id desc limit 1
`;

const CELLS_SQL = `
  select coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) as listing_id,
         r.payload->>'availability' as availability,
         r.payload->>'gppe' as gppe,
         r.payload->>'confidence' as confidence
  from mart.topt_gppe_results r
  left join lateral (
    select ei.listing_id
    from mart.entity_identity ei
    where ei.entity_id::text = (r.payload->>'listing_id')
       or (ei.entity_id::text = r.issuer_id and ei.kind = 'issuer')
    order by (ei.entity_id::text = (r.payload->>'listing_id')) desc
    limit 1
  ) ei on true
  where r.run_id = $1
  order by coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) limit $2
`;

const QUALITY_SQL = `
  select payload from mart.datahub_quality_report where run_id = $1 order by created_at desc limit 1
`;

const REQUESTED_COUNT_SQL = `
  select obligation_count from mart.topt_capture_status where run_id = $1
`;

class SchemaMismatchError extends Error {}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") throw new SchemaMismatchError(`${field} is not a string`);
  return value;
}

function optionalString(value: unknown, field: string): string | null {
  return value === null ? null : requireString(value, field);
}

function cellFromRow(row: Record<string, unknown>): ToptGppeCell {
  return {
    listing_id: requireString(row.listing_id, "listing_id"),
    availability: requireString(row.availability, "availability"),
    gppe: optionalString(row.gppe, "gppe"),
    confidence: optionalString(row.confidence, "confidence"),
  };
}

function qualityFromRow(row: Record<string, unknown> | undefined): Record<string, unknown> | null {
  if (row === undefined) return null;
  const payload = row.payload;
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new SchemaMismatchError("quality report payload is not a JSON object");
  }
  return payload as Record<string, unknown>;
}

export class MartToptGppeRepository {
  constructor(
    private readonly runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
  ) {}

  async latest(limit = 100): Promise<ToptGppeReport | ToptGppeUnavailable> {
    if (!(limit >= 1 && limit <= 500)) {
      throw new RangeError("limit must be between 1 and 500");
    }

    return this.runWithClient(async (client) => {
      const servedUniverseLike = idPrefixLikeOfRole("strategy");
      let head = await client.query(POINTER_HEAD_SQL, [servedUniverseLike]);
      if (head.rows.length > 0) {
        const row = head.rows[0];
        if (row.availability === "unavailable" || row.run_id === null) {
          const reason =
            typeof row.staleness_reason === "string" && row.staleness_reason.length > 0
              ? row.staleness_reason
              : "older_than_30d";
          return { reason: `head_unavailable: ${reason}` };
        }
      } else {
        head = await client.query(ACCEPTANCE_FALLBACK_HEAD_SQL, [servedUniverseLike]);
      }
      if (head.rows.length === 0 || head.rows[0].run_id === null) {
        return { reason: "no accepted (quality-reported) production TOPT run" };
      }

      try {
        const runId = requireString(head.rows[0].run_id, "run_id");
        const cellRows = await client.query(CELLS_SQL, [runId, limit]);
        const qualityRows = await client.query(QUALITY_SQL, [runId]);
        const statusRows = await client.query(REQUESTED_COUNT_SQL, [runId]);
        if (statusRows.rows.length === 0) return { reason: "no capture status for the governed run" };
        const requestedCount = statusRows.rows[0].obligation_count;
        if (typeof requestedCount !== "number" || !Number.isInteger(requestedCount)) {
          throw new SchemaMismatchError("obligation_count is not an integer");
        }
        const cells = cellRows.rows.map(cellFromRow);
        return {
          run_id: runId,
          requested_count: requestedCount,
          available_count: cells.filter((cell) => cell.availability === "available").length,
          cells,
          quality: qualityFromRow(qualityRows.rows[0]),
        };
      } catch (error) {
        if (error instanceof SchemaMismatchError) return { reason: `schema_mismatch: ${error.message}` };
        throw error;
      }
    });
  }
}
