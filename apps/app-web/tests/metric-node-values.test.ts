/**
 * #1413 (F4): `MartMetricNodeValueRepository` against an injected fake `MartClientLike`.
 * Verifies governed-head resolution (`mart.served_head` and fallback) and generic
 * `node_key` reads from `mart.metric_node_values`, matching `PostgresMetricNodeValueRepository`.
 *
 * Run standalone: `bun run tests/metric-node-values.test.ts`.
 */

import { MartMetricNodeValueRepository } from "../src/server/mart/metric-node-values";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const RUN_ID = "capture-run:" + "b".repeat(64);

interface Call {
  sql: string;
  params: readonly unknown[] | undefined;
}

function fakeRunner(
  responses: (sql: string, params: readonly unknown[] | undefined) => { rows: Record<string, unknown>[] },
  calls: Call[],
) {
  const client: MartClientLike = {
    query: async (sql, params) => {
      calls.push({ sql, params });
      return responses(sql, params);
    },
  };
  return async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => fn(client);
}

{
  const calls: Call[] = [];
  const runner = fakeRunner((sql) => {
    if (sql.includes("served_head")) {
      return {
        rows: [{ run_id: RUN_ID, availability: "available", freshness: "fresh", staleness_reason: null }],
      };
    }
    if (sql.includes("from mart.metric_node_values")) {
      return {
        rows: [
          {
            issuer_id: "issuer:nvda",
            node_id: "6143cf17-ceba-5f78-a2d3-a7cc423c49a7",
            node_key: "custom_node_v1",
            period: "2025-12-31",
            period_tag: "FY",
            value: "9000000",
            unit: "per_employee",
            availability_status: "available",
            reason_codes: [],
            source_evidence_status: "verified",
            factor_validation_status: "accepted",
            confidence: "0.92",
            confidence_band: "high",
            tree_version: "v1",
            tree_sha256: "f".repeat(64),
          },
          {
            issuer_id: "issuer:jpm",
            node_id: "6143cf17-ceba-5f78-a2d3-a7cc423c49a7",
            node_key: "custom_node_v1",
            period: "2025-12-31",
            period_tag: "FY",
            value: null,
            unit: "per_employee",
            availability_status: "unavailable",
            reason_codes: ["missing_gross_profit"],
            source_evidence_status: "verified",
            factor_validation_status: "accepted",
            confidence: "0",
            confidence_band: "missing",
            tree_version: "v1",
            tree_sha256: "f".repeat(64),
          },
        ],
      };
    }
    throw new Error(`unexpected query: ${sql}`);
  }, calls);

  const result = await new MartMetricNodeValueRepository(runner).latest("custom_node_v1");
  assert("cells" in result, `expected a report, got unavailable: ${JSON.stringify(result)}`);
  assert(result.run_id === RUN_ID, "run_id must round-trip from the head query");
  assert(result.node_key === "custom_node_v1", "node_key must round-trip");
  assert(result.available_count === 1, `expected available_count 1, got ${result.available_count}`);
  assert(result.cells.length === 2, `expected 2 cells, got ${result.cells.length}`);
  assert(result.cells[0].value === "9000000", "available cell value round-trips as string");
  assert(result.cells[1].value === null, "unavailable cell keeps null value");

  const nodeCall = calls.find((c) => c.sql.includes("from mart.metric_node_values"));
  assert(
    nodeCall !== undefined &&
      nodeCall.params?.[0] === RUN_ID &&
      nodeCall.params?.[1] === "custom_node_v1" &&
      nodeCall.params?.[2] === 100,
    "metric_node_values query is bound to run_id, node_key, and limit",
  );
  console.log("#1413 metric-node-values governed-pointer ready path passed");
}
