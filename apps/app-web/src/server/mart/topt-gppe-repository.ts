/**
 * #433: TypeScript mirror of `truealpha_contracts.topt_read.PostgresToptGppeRepository`
 * (the class the deployed MCP `topt_gppe` tool calls, unconditionally — no fixture
 * gate). The SQL here is copied byte-for-byte from the Python source so the App and
 * MCP resolve the same governed head and return the same values; when that source
 * changes, mirror the change here too.
 *
 * Head resolution deliberately does NOT use `mart.current_pointer` (#378: the
 * evidence-graph plane is not yet wired into the capture path, so the pointer is
 * empty). It reads `mart.topt_capture_status` (a view over the raw capture-control
 * tables — no fixture involved) joined to the most recently accepted
 * `datahub_quality_report`, exactly as the Python interim comment describes.
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

const HEAD_SQL = `
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

// Mirrors the Python repository's hardcoded curated-universe size — matches the
// `observation_count = 84` invariant enforced on staging.topt_core_snapshots.
const REQUESTED_COUNT = 84;

function cellFromRow(row: Record<string, unknown>): ToptGppeCell {
  return {
    listing_id: String(row.listing_id),
    availability: String(row.availability),
    gppe: row.gppe === null ? null : String(row.gppe),
    confidence: row.confidence === null ? null : String(row.confidence),
  };
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
      const head = await client.query(HEAD_SQL);
      if (head.rows.length === 0) {
        return { reason: "no accepted (quality-reported) production TOPT run" };
      }
      const runId = String(head.rows[0].run_id);

      const [cellRows, qualityRows] = await Promise.all([
        client.query(CELLS_SQL, [runId, limit]),
        client.query(QUALITY_SQL, [runId]),
      ]);

      const cells = cellRows.rows.map(cellFromRow);
      return {
        run_id: runId,
        requested_count: REQUESTED_COUNT,
        available_count: cells.filter((cell) => cell.availability === "available").length,
        cells,
        quality: qualityRows.rows.length > 0 ? (qualityRows.rows[0].payload as Record<string, unknown>) : null,
      };
    });
  }
}
