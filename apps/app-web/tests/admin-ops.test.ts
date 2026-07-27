/**
 * #495 (surface 2b): loadOpsOverview outcomes — deny-before-query for
 * anonymous and non-administrator principals, role assumption + shapes on
 * the happy path, and honest degradation when dagster.runs does not exist.
 * Same DI style as tests/strategy-page.test.ts (fake client, no Postgres).
 *
 * Run standalone: `bun run tests/admin-ops.test.ts`.
 */

import { __setTestOpsClient, loadOpsOverview } from "../src/server/admin/ops";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// --- denied: anonymous and member, the client must never be touched ---
{
  let touched = false;
  __setTestOpsClient({
    query: async () => {
      touched = true;
      throw new Error("must not query before authorization");
    },
  } as never);
  const anon = await loadOpsOverview(null);
  const member = await loadOpsOverview({ principalKind: "member" });
  assert(anon.kind === "denied" && member.kind === "denied", "non-admins must be denied");
  assert(!touched, "no query may run before the deny decision");
}

// --- ready: role assumed, three sections shaped ---
{
  const queries: string[] = [];
  __setTestOpsClient({
    query: async (sql: string) => {
      queries.push(sql);
      if (sql.includes("dagster.runs")) {
        return {
          rows: [
            {
              run_id: "abc123",
              pipeline_name: "topt_live_pipeline",
              status: "SUCCESS",
              create_timestamp: "2026-07-27T22:15:03Z",
              start_time: 100,
              end_time: 352,
            },
          ],
        };
      }
      if (sql.includes("current_pointer_head")) {
        return {
          rows: [{ target_run_id: "capture-run:e".padEnd(20, "e"), sequence: 7, advanced_at: "2026-07-27T22:19:00Z" }],
        };
      }
      if (sql.includes("raw.fetches")) {
        return { rows: [{ source: "twelve-data", fetches: 21 }] };
      }
      return { rows: [] };
    },
  } as never);

  const outcome = await loadOpsOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(queries[0] === "set role app_ops_reader", "the ops role must be assumed first");
  assert(outcome.data.runs !== "unavailable" && outcome.data.runs[0].durationSeconds === 252, "duration = end - start");
  assert(outcome.data.pointer !== null && outcome.data.pointer.sequence === 7, "pointer shape");
  assert(outcome.data.quotaToday[0].source === "twelve-data" && outcome.data.quotaToday[0].fetches === 21, "quota shape");
}

// --- honest degradation: dagster.runs missing -> runs: "unavailable", rest intact ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.includes("dagster.runs")) throw new Error('relation "dagster.runs" does not exist');
      if (sql.includes("current_pointer_head")) return { rows: [] };
      if (sql.includes("raw.fetches")) return { rows: [] };
      return { rows: [] };
    },
  } as never);
  const outcome = await loadOpsOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", "a missing dagster schema must not fail the page");
  assert(outcome.data.runs === "unavailable", "runs must degrade to unavailable, not an empty lie");
  assert(outcome.data.pointer === null, "no pointer row -> null");
}

__setTestOpsClient(null);
console.log("#495 admin-ops loader outcomes passed");
