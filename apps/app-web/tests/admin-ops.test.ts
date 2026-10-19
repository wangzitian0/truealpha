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
      if (sql.includes("served_head")) {
        // Universes in alphabetical order: canary, topt-core, and an unscheduled staging QQQ head.
        return {
          rows: [
            { environment: "production", universe_id: "canary", target_run_id: "capture-run:c".padEnd(20, "c"), sequence: 3, advanced_at: "2026-07-20T22:19:00Z", age_hours: 12.5, freshness: "fresh", availability: "available" },
            { environment: "production", universe_id: "topt-core", target_run_id: "capture-run:e".padEnd(20, "e"), sequence: 7, advanced_at: "2026-07-27T22:19:00Z", age_hours: 4.2, freshness: "fresh", availability: "available" },
            { environment: "staging", universe_id: "universe:qqq-us-t5", target_run_id: "capture-run:q".padEnd(20, "q"), sequence: 1, advanced_at: "2026-07-10T22:19:00Z", age_hours: 170.0, freshness: "stale", availability: "available", staleness_reason: "older_than_3d" },
          ],
        };
      }
      if (sql.includes("data_engine_identity")) {
        return {
          rows: [{ run_id: "capture-run:e".padEnd(20, "e"), git_sha: "4cf7291deadbeef", image_digest: "sha256:00f7", created_at: "2026-07-27T22:15:03Z" }],
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
  assert(outcome.data.pointers.length === 3, "one pointer row per universe, none collapsed");
  assert(
    outcome.data.pointers[1].universeId === "topt-core" && outcome.data.pointers[1].sequence === 7,
    "the core universe survives next to the canary",
  );
  assert(outcome.data.pointers[1].ageHours === 4.2 && outcome.data.pointers[1].freshness === "fresh", "age and freshness passed through");
  assert(
    outcome.data.pointers[2].universeId === "universe:qqq-us-t5" &&
      outcome.data.pointers[2].freshness === "manual" &&
      outcome.data.pointers[2].stalenessReason === "older_than_3d",
    "unscheduled staging universe:qqq-% head is downgraded to manual freshness (#1359, #1363)",
  );
  assert(outcome.data.dataEngine !== null && outcome.data.dataEngine.gitSha === "4cf7291deadbeef", "data-engine build shape");
  assert(outcome.data.quotaToday[0].source === "twelve-data" && outcome.data.quotaToday[0].fetches === 21, "quota shape");
  const quotaSql = queries.find((q) => q.includes("from raw.fetches"));
  assert(
    quotaSql !== undefined &&
      quotaSql.includes("at time zone 'utc'") &&
      quotaSql.includes("-[0-9]{8}T[0-9]{4}$"),
    "quotaToday query uses explicit UTC day boundary and excludes legacy run-suffixed source strings",
  );
}

// --- honest degradation: dagster.runs missing -> runs: "unavailable", rest intact ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.includes("dagster.runs")) throw new Error('relation "dagster.runs" does not exist');
      if (sql.includes("served_head")) return { rows: [] };
      if (sql.includes("data_engine_identity")) throw new Error('relation "mart.data_engine_identity" does not exist');
      if (sql.includes("raw.fetches")) return { rows: [] };
      return { rows: [] };
    },
  } as never);
  const outcome = await loadOpsOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", "a missing dagster schema must not fail the page");
  assert(outcome.data.runs === "unavailable", "runs must degrade to unavailable, not an empty lie");
  assert(outcome.data.pointers.length === 0, "no pointer row -> no cards, not a fabricated one");
  assert(outcome.data.dataEngine === null, "a database without the identity view reports the build as unknown");
}

__setTestOpsClient(null);
console.log("#495 admin-ops loader outcomes passed");
