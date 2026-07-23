/**
 * #433: TypeScript mirror of `truealpha_contracts.topt_read.PostgresToptGppeRepository`
 * (the class the deployed MCP `topt_gppe` tool calls, unconditionally — no fixture
 * gate). The SQL here is copied byte-for-byte from the Python source so the App and
 * MCP resolve the same governed head and return the same values; when that source
 * changes, mirror the change here too.
 *
 * #434 P4 follow-up: head resolution now mirrors the Python two-step resolver —
 * `mart.current_pointer_head` (the ADR-A1 governed head; #378 wires the evidence
 * graph that advances it) first, falling back to the acceptance-gated
 * `topt_capture_status`/`datahub_quality_report` join only when no pointer has
 * advanced yet for this (environment, factor_id). Previously this always used the
 * fallback query, so the App and MCP could silently resolve different governed
 * heads once the pointer started advancing.
 *
 * Server-only; never import into a client component.
 */

import type { ToptGppeCell, ToptGppeReport, ToptGppeUnavailable } from "@/contracts/toptGppe";
import { withMartReadonly } from "./db";

/** The subset of `pg`'s `PoolClient` this repository needs — narrow enough that
 * tests can inject a fake without constructing a real connection. */
export interface MartClientLike {
  query(sql: string, params?: readonly unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

const POINTER_HEAD_SQL = `
  select target_run_id as run_id from mart.current_pointer_head
  where environment = 'production' and factor_id = 'gross_profit_per_employee'
  order by advanced_at desc limit 1
`;

const ACCEPTANCE_FALLBACK_HEAD_SQL = `
  select s.run_id
  from mart.topt_capture_status s
  join mart.datahub_quality_report q on q.run_id = s.run_id
  where s.environment = 'production' and s.complete
  order by q.created_at desc, q.report_id desc limit 1
`;

const CELLS_SQL = `
  select payload->>'listing_id' as listing_id,
         payload->>'availability' as availability,
         payload->>'gppe' as gppe,
         payload->>'confidence' as confidence
  from mart.topt_gppe_results
  where payload->>'run_id' = $1
  order by payload->>'listing_id' limit $2
`;

const QUALITY_SQL = `
  select payload from mart.datahub_quality_report where run_id = $1 order by created_at desc limit 1
`;

// The requested-cell denominator comes from the capture plane —
// mart.topt_capture_status.obligation_count IS the run's requested cells —
// never a constant: the hardcoded 84 made any run over a different universe
// self-contradict its own quality payload (#462 AC3; identical query in the
// Python twin, truealpha_contracts.topt_read).
const REQUESTED_COUNT_SQL = `
  select obligation_count from mart.topt_capture_status where run_id = $1
`;

class SchemaMismatchError extends Error {}

/** Fails closed on an unexpected row shape instead of `String(...)` coercing it — a bare
 * `String(null)`/`String(undefined)` would silently produce the *string* "null"/"undefined"
 * rather than surfacing the mismatch (Copilot review on #437). */
function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") throw new SchemaMismatchError(`${field} is not a string`);
  return value;
}

function optionalString(value: unknown, field: string): string | null {
  if (value === null) return null;
  return requireString(value, field);
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
  /** `runWithClient` is an injection point for tests only (a fake `MartClientLike`,
   * no real connection); production callers omit it and get the real `mart_readonly`
   * session via `withMartReadonly`. */
  constructor(
    private readonly runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
  ) {}

  async latest(limit = 100): Promise<ToptGppeReport | ToptGppeUnavailable> {
    if (!(limit >= 1 && limit <= 500)) {
      throw new RangeError("limit must be between 1 and 500");
    }

    return this.runWithClient(async (client) => {
      let head = await client.query(POINTER_HEAD_SQL);
      if (head.rows.length === 0) {
        head = await client.query(ACCEPTANCE_FALLBACK_HEAD_SQL);
      }
      if (head.rows.length === 0) {
        return { reason: "no accepted (quality-reported) production TOPT run" };
      }

      try {
        const runId = requireString(head.rows[0].run_id, "run_id");

        const [cellRows, qualityRows, statusRows] = await Promise.all([
          client.query(CELLS_SQL, [runId, limit]),
          client.query(QUALITY_SQL, [runId]),
          client.query(REQUESTED_COUNT_SQL, [runId]),
        ]);

        if (statusRows.rows.length === 0) {
          return { reason: "no capture status for the governed run" };
        }
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
