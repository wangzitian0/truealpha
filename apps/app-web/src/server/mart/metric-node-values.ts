/**
 * #1413 (F4): TypeScript mirror of `truealpha_contracts.metric_node_read.PostgresMetricNodeValueRepository`.
 *
 * Reads `mart.metric_node_values` by `node_key` at the governed head (`mart.served_head` where
 * `universe_id` starts with the id prefix of the universe with role `strategy`, falling back to the acceptance-gated TOPT run when
 * no pointer has advanced yet). Both App and MCP resolve the same head and return identical rows.
 *
 * Server-only; never import into a client component.
 */

import { idPrefixLikeOfRole } from "@/contracts/universes";
import { withMartReadonly } from "./db";
import type { MartClientLike } from "./topt-gppe-repository";

export interface MetricNodeValueCell {
  issuer_id: string;
  node_id: string;
  node_key: string;
  period: string;
  period_tag: string | null;
  value: string | null;
  unit: string | null;
  availability_status: string;
  reason_codes: readonly string[];
  source_evidence_status: string;
  factor_validation_status: string;
  confidence: string | null;
  confidence_band: string | null;
  tree_version: string | null;
  tree_sha256: string | null;
}

export interface MetricNodeValueReport {
  run_id: string;
  node_key: string;
  available_count: number;
  cells: MetricNodeValueCell[];
}

export interface MetricNodeValueUnavailable {
  node_key: string;
  reason: string;
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

const NODE_VALUES_BY_KEY_SQL = `
  select v.issuer_id,
         v.node_id::text as node_id,
         v.node_key,
         v.period,
         v.period_tag,
         v.value::text as value,
         v.unit,
         v.availability_status,
         v.reason_codes,
         v.source_evidence_status,
         v.factor_validation_status,
         v.confidence::text as confidence,
         v.confidence_band,
         v.tree_version,
         v.tree_sha256
  from mart.metric_node_values v
  where v.run_id = $1
    and v.node_key = $2
  order by v.issuer_id, v.period desc limit $3
`;

const NODE_VALUES_BY_KEY_AND_ISSUER_SQL = `
  select v.issuer_id,
         v.node_id::text as node_id,
         v.node_key,
         v.period,
         v.period_tag,
         v.value::text as value,
         v.unit,
         v.availability_status,
         v.reason_codes,
         v.source_evidence_status,
         v.factor_validation_status,
         v.confidence::text as confidence,
         v.confidence_band,
         v.tree_version,
         v.tree_sha256
  from mart.metric_node_values v
  where v.run_id = $1
    and v.node_key = $2
    and v.issuer_id = $3
  order by v.issuer_id, v.period desc limit $4
`;

class SchemaMismatchError extends Error {}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") throw new SchemaMismatchError(`${field} is not a string`);
  return value;
}

function optionalString(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null;
  return requireString(value, field);
}

function stringArray(value: unknown, field: string): readonly string[] {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new SchemaMismatchError(`${field} is not a string array`);
  }
  return value;
}

function cellFromRow(row: Record<string, unknown>): MetricNodeValueCell {
  return {
    issuer_id: requireString(row.issuer_id, "issuer_id"),
    node_id: requireString(row.node_id, "node_id"),
    node_key: requireString(row.node_key, "node_key"),
    period: requireString(row.period, "period"),
    period_tag: optionalString(row.period_tag, "period_tag"),
    value: optionalString(row.value, "value"),
    unit: optionalString(row.unit, "unit"),
    availability_status: requireString(row.availability_status, "availability_status"),
    reason_codes: stringArray(row.reason_codes, "reason_codes"),
    source_evidence_status: requireString(row.source_evidence_status, "source_evidence_status"),
    factor_validation_status: requireString(row.factor_validation_status, "factor_validation_status"),
    confidence: optionalString(row.confidence, "confidence"),
    confidence_band: optionalString(row.confidence_band, "confidence_band"),
    tree_version: optionalString(row.tree_version, "tree_version"),
    tree_sha256: optionalString(row.tree_sha256, "tree_sha256"),
  };
}

export class MartMetricNodeValueRepository {
  constructor(
    private readonly runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
  ) {}

  async latest(
    nodeKey: string,
    options?: { issuerId?: string; limit?: number },
  ): Promise<MetricNodeValueReport | MetricNodeValueUnavailable> {
    if (!nodeKey || nodeKey.trim().length === 0) {
      throw new RangeError("nodeKey must be non-empty");
    }
    const limit = options?.limit ?? 100;
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
          return { node_key: nodeKey, reason: `head_unavailable: ${reason}` };
        }
      } else {
        head = await client.query(ACCEPTANCE_FALLBACK_HEAD_SQL, [servedUniverseLike]);
      }
      if (head.rows.length === 0 || head.rows[0].run_id === null) {
        return { node_key: nodeKey, reason: "no accepted (quality-reported) production TOPT run" };
      }

      try {
        const runId = requireString(head.rows[0].run_id, "run_id");
        const issuerId = options?.issuerId;
        const cellRows =
          issuerId !== undefined
            ? await client.query(NODE_VALUES_BY_KEY_AND_ISSUER_SQL, [runId, nodeKey, issuerId, limit])
            : await client.query(NODE_VALUES_BY_KEY_SQL, [runId, nodeKey, limit]);
        const cells = cellRows.rows.map(cellFromRow);
        return {
          run_id: runId,
          node_key: nodeKey,
          available_count: cells.filter((cell) => cell.availability_status === "available").length,
          cells,
        };
      } catch (error) {
        if (error instanceof SchemaMismatchError) {
          return { node_key: nodeKey, reason: `schema_mismatch: ${error.message}` };
        }
        throw error;
      }
    });
  }
}
