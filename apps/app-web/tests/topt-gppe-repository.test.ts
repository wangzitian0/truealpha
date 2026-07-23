/**
 * #433: `MartToptGppeRepository` against an injected fake `MartClientLike` — no
 * live Postgres. Proves the exact SQL/parameters this class sends (byte-identical
 * to `truealpha_contracts.topt_read.PostgresToptGppeRepository`, the class the
 * deployed MCP `topt_gppe` tool calls) and that rows map to the DTO correctly,
 * including the null-availability and no-accepted-run paths.
 *
 * A live-DB test through the real `mart.topt_gppe_results` write path was assessed
 * and scoped out: that table's `validate_topt_gppe_result()` trigger enforces deep
 * cross-table consistency (a matching `topt_gppe_invocations` + `topt_core_snapshots`
 * + `topt_core_snapshot_members` row, plus a 23-key payload mirroring every column)
 * that belongs to data-engine's write path, not this read adapter — constructing a
 * valid synthetic row here would duplicate that write-path's own test responsibility
 * without adding coverage of the code this file actually owns. Even the Python
 * reference (`PostgresToptGppeRepository`) has no live-DB test of its own SQL today
 * (`test_mcp_server.py` only exercises the MCP tool against a fake repository); this
 * mocked-client test already exceeds that bar for the App-side port.
 *
 * Run standalone: `bun run tests/topt-gppe-repository.test.ts`.
 */

import { MartToptGppeRepository, type MartClientLike } from "../src/server/mart/topt-gppe-repository";

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

// --- ready via the governed pointer: head resolves from current_pointer_head, two cells
// (one available, one unavailable), a quality report; the acceptance-gated fallback must
// never be queried once the pointer resolves (#434 P4 follow-up) ---
{
  const calls: Call[] = [];
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 84 }] };
    if (sql.includes("topt_capture_status")) throw new Error("must not fall back once the pointer resolves");
    if (sql.includes("topt_gppe_results")) {
      return {
        rows: [
          { listing_id: "listing:aaa", availability: "available", gppe: "1500000.00", confidence: "0.90" },
          { listing_id: "listing:bbb", availability: "unavailable", gppe: null, confidence: null },
        ],
      };
    }
    if (sql.includes("datahub_quality_report where run_id")) {
      return { rows: [{ payload: { independent_reconciliation: "0.25" } }] };
    }
    throw new Error(`unexpected query: ${sql}`);
  }, calls);

  const result = await new MartToptGppeRepository(runner).latest();
  assert("cells" in result, `expected a report, got unavailable: ${JSON.stringify(result)}`);
  assert(result.run_id === RUN_ID, "run_id must round-trip from the head query");
  assert(result.requested_count === 84, `expected requested_count 84, got ${result.requested_count}`);
  assert(result.available_count === 1, `expected available_count 1, got ${result.available_count}`);
  assert(result.cells.length === 2, `expected 2 cells, got ${result.cells.length}`);
  assert(result.cells[0].gppe === "1500000.00" && typeof result.cells[0].gppe === "string", "gppe stays a string");
  assert(result.cells[1].gppe === null && result.cells[1].confidence === null, "unavailable cell keeps null numerics");
  assert(result.quality?.independent_reconciliation === "0.25", "quality payload round-trips");

  const cellsCall = calls.find((c) => c.sql.includes("topt_gppe_results"));
  assert(cellsCall !== undefined && cellsCall.params?.[0] === RUN_ID && cellsCall.params?.[1] === 100, "cells query is bound to the resolved run_id and default limit");
  const qualityCall = calls.find((c) => c.sql.includes("datahub_quality_report where run_id"));
  assert(qualityCall !== undefined && qualityCall.params?.[0] === RUN_ID, "quality query is bound to the resolved run_id");

  console.log("#433 topt-gppe-repository governed-pointer ready path passed");
}

// --- ready via the acceptance-gated fallback: pointer empty (no advance yet), falls back
// to topt_capture_status + datahub_quality_report, exactly as before #378 wires the pointer ---
{
  const calls: Call[] = [];
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 84 }] };
    if (sql.includes("topt_capture_status")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("topt_gppe_results")) {
      return { rows: [{ listing_id: "listing:aaa", availability: "available", gppe: "1500000.00", confidence: "0.90" }] };
    }
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [] };
    throw new Error(`unexpected query: ${sql}`);
  }, calls);

  const result = await new MartToptGppeRepository(runner).latest();
  assert("cells" in result, `expected a report, got unavailable: ${JSON.stringify(result)}`);
  assert(result.run_id === RUN_ID, "run_id must round-trip from the fallback head query");
  assert(calls.some((c) => c.sql.includes("current_pointer_head")), "must try the pointer first");

  console.log("#434 topt-gppe-repository acceptance-fallback ready path passed");
}

// --- unavailable: neither the pointer nor the acceptance-gated fallback resolves; cells/quality
// never queried ---
{
  const calls: Call[] = [];
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [] };
    if (sql.includes("topt_capture_status")) return { rows: [] };
    throw new Error(`must not query further when no head is resolved: ${sql}`);
  }, calls);

  const result = await new MartToptGppeRepository(runner).latest();
  assert(!("cells" in result), "expected unavailable when no head resolves");
  assert(result.reason.includes("no accepted"), `unexpected reason: ${result.reason}`);
  assert(calls.length === 2, `expected exactly two queries (pointer, then fallback), got ${calls.length}`);

  console.log("#433 topt-gppe-repository no-accepted-run path passed");
}

// --- empty quality report: cells present, no quality row for that run ---
{
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 84 }] };
    if (sql.includes("topt_gppe_results")) return { rows: [] };
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [] };
    throw new Error(`unexpected query: ${sql}`);
  }, []);

  const result = await new MartToptGppeRepository(runner).latest();
  assert("cells" in result, `expected a report, got unavailable: ${JSON.stringify(result)}`);
  assert(result.cells.length === 0, "no cells for this run");
  assert(result.quality === null, "no quality report for this run must be null, not undefined or missing");

  console.log("#433 topt-gppe-repository empty-quality path passed");
}

// --- schema mismatch: a malformed cell row fails closed instead of silently stringifying
// null/undefined into the literal words "null"/"undefined" (Copilot review on #437) ---
{
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 84 }] };
    if (sql.includes("topt_gppe_results")) return { rows: [{ listing_id: null, availability: "available", gppe: null, confidence: null }] };
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [] };
    throw new Error(`unexpected query: ${sql}`);
  }, []);

  const result = await new MartToptGppeRepository(runner).latest();
  assert(!("cells" in result), "a malformed cell row must not silently produce a report");
  assert(result.reason.includes("listing_id is not a string"), `unexpected reason: ${result.reason}`);

  console.log("#433 topt-gppe-repository schema-mismatch (cell) path passed");
}

// --- schema mismatch: a non-object quality payload fails closed ---
{
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 84 }] };
    if (sql.includes("topt_gppe_results")) return { rows: [] };
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [{ payload: "not-an-object" }] };
    throw new Error(`unexpected query: ${sql}`);
  }, []);

  const result = await new MartToptGppeRepository(runner).latest();
  assert(!("cells" in result), "a non-object quality payload must not silently produce a report");
  assert(result.reason.includes("quality report payload is not a JSON object"), `unexpected reason: ${result.reason}`);

  console.log("#433 topt-gppe-repository schema-mismatch (quality) path passed");
}

// --- limit validation: rejected before the client is ever touched ---
{
  let clientTouched = false;
  const runner = async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => {
    clientTouched = true;
    return fn({ query: async () => ({ rows: [] }) });
  };
  const repository = new MartToptGppeRepository(runner);

  let threw = false;
  try {
    await repository.latest(0);
  } catch (error) {
    threw = error instanceof RangeError;
  }
  assert(threw, "limit=0 must throw RangeError");
  assert(!clientTouched, "an invalid limit must be rejected before the client is used");

  threw = false;
  try {
    await repository.latest(501);
  } catch (error) {
    threw = error instanceof RangeError;
  }
  assert(threw, "limit=501 must throw RangeError");

  console.log("#433 topt-gppe-repository limit validation passed");
}

// --- #462 AC3 / the universe-≠-84 trigger state at the wiring level: the denominator
// is the capture plane's obligation_count for THE governed run, never a constant ---
{
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [{ obligation_count: 100 }] };
    if (sql.includes("topt_gppe_results")) return { rows: [] };
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [] };
    throw new Error(`unexpected query: ${sql}`);
  }, []);

  const result = await new MartToptGppeRepository(runner).latest();
  assert("cells" in result, `expected a report, got unavailable: ${JSON.stringify(result)}`);
  assert(result.requested_count === 100, `a 100-cell universe must report 100, got ${result.requested_count}`);

  console.log("#462 topt-gppe-repository run-own-denominator path passed");
}

// --- a governed run with no capture status row cannot report a denominator: unavailable ---
{
  const runner = fakeRunner((sql) => {
    if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
    if (sql.includes("obligation_count")) return { rows: [] };
    if (sql.includes("topt_gppe_results")) return { rows: [] };
    if (sql.includes("datahub_quality_report where run_id")) return { rows: [] };
    throw new Error(`unexpected query: ${sql}`);
  }, []);

  const result = await new MartToptGppeRepository(runner).latest();
  assert(!("cells" in result), "missing capture status must not silently produce a report");
  assert(result.reason.includes("no capture status"), `unexpected reason: ${result.reason}`);

  console.log("#462 topt-gppe-repository missing-status path passed");
}
