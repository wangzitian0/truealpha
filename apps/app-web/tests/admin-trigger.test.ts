/**
 * #495's manual trigger, with #874's forced fetch: requestPipelineTrigger denies
 * before it touches the database, writes the operator's `force_fetch` into the
 * request row the data-engine sensor reads, and refuses a malformed flag instead
 * of coercing it. A fake runtime stands in for `withAppRuntime`, so no Postgres
 * is needed (the grant and the sensor half are
 * apps/data-engine/tests/test_pipeline_trigger_sensor.py).
 *
 * Run standalone: `bun run tests/admin-trigger.test.ts`.
 */

import { __setTestTriggerRuntime, requestPipelineTrigger } from "../src/server/admin/trigger";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

interface Call {
  sql: string;
  params: unknown[];
}

function recordingRuntime(calls: Call[]) {
  const client = {
    query: async (sql: string, params: unknown[] = []) => {
      calls.push({ sql, params });
      return { rows: [{ request_id: "42" }] };
    },
  };
  return async <T>(fn: (injected: never) => Promise<T>): Promise<T> => fn(client as never);
}

const administrator = { principalId: "principal:owner", principalKind: "administrator" as const };

// --- denied: a member never reaches the database, forced or not ---
{
  const calls: Call[] = [];
  __setTestTriggerRuntime(recordingRuntime(calls));
  const outcome = await requestPipelineTrigger({ principalId: "principal:m", principalKind: "member" }, undefined, true);
  assert(outcome.kind === "denied", `a member must be denied, got ${outcome.kind}`);
  assert(calls.length === 0, "no insert may run before the deny decision");
}

// --- an ordinary request: the pre-#874 statement, so it also works on a schema the
// migration has not reached yet (migrations apply when llm-service boots) ---
for (const unforced of [undefined, false]) {
  const calls: Call[] = [];
  __setTestTriggerRuntime(recordingRuntime(calls));
  const outcome = await requestPipelineTrigger(administrator, "2026-09-16T21:30:00Z", unforced);
  assert(outcome.kind === "accepted", `expected accepted, got ${outcome.kind}`);
  assert(outcome.forceFetch === false, "an unasked request is not forced");
  assert(outcome.requestId === 42 && outcome.executedAt === "2026-09-16T21:30:00.000Z", "identity echoed");
  assert(calls.length === 1, "exactly one insert");
  assert(!/\bforce_fetch\b/.test(calls[0].sql), "an unforced insert leaves force_fetch to its false default");
  assert(!calls[0].params.includes(true), "nothing forced is bound");
}

// --- a forced request: the flag reaches the row the sensor reads ---
{
  const calls: Call[] = [];
  __setTestTriggerRuntime(recordingRuntime(calls));
  const outcome = await requestPipelineTrigger(administrator, undefined, true);
  assert(outcome.kind === "accepted" && outcome.forceFetch === true, "a forced request is accepted as forced");
  assert(/\bforce_fetch\b/.test(calls[0].sql), "a forced insert names force_fetch");
  assert(calls[0].params.length === 4 && calls[0].params[3] === true, "force_fetch is bound true");
  assert(calls[0].sql.includes("staging.pipeline_trigger_requests"), "the Postgres-mediated trigger table");
}

// --- a malformed flag is refused, never coerced into a forced run ---
for (const malformed of ["true", 1, null, {}]) {
  const calls: Call[] = [];
  __setTestTriggerRuntime(recordingRuntime(calls));
  const outcome = await requestPipelineTrigger(administrator, undefined, malformed);
  assert(outcome.kind === "invalid", `force_fetch=${JSON.stringify(malformed)} must be invalid, got ${outcome.kind}`);
  assert(outcome.message.includes("force_fetch"), "the message names the field");
  assert(calls.length === 0, "nothing is inserted for an invalid request");
}

__setTestTriggerRuntime(null);
console.log("admin-trigger: all assertions passed");
