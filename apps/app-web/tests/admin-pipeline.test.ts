/**
 * #1466: loadPipelineOverview outcomes — deny-before-query for anonymous and member principals,
 * per-job counts and rates, the first error line of the newest failed run, the two verdicts and the
 * release age, and honest absence when Dagster has no tables or no run.
 * Same DI style as tests/admin-ops.test.ts (fake client, no Postgres).
 *
 * Run standalone: `bun run tests/admin-pipeline.test.ts`.
 */

import { RELEASE_START_SQL, __setTestOpsClient } from "../src/server/admin/ops";
import { FAILURE_EVENTS_SQL, JOBS_SQL, TIMEOUTS_SQL, firstErrorLine, loadPipelineOverview } from "../src/server/admin/pipeline";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

/** A STEP_FAILURE event as Dagster 1.13 serializes it: the op's exception is the innermost cause. */
const STEP_FAILURE = JSON.stringify({
  __class__: "EventLogEntry",
  dagster_event: {
    __class__: "DagsterEvent",
    event_type_value: "STEP_FAILURE",
    message: 'Execution of step "run_supply_chain_exposure" failed.',
    event_specific_data: {
      __class__: "StepFailureData",
      error: {
        __class__: "SerializableErrorInfo",
        message:
          'dagster._core.errors.DagsterExecutionStepExecutionError: Error occurred while executing op "run_supply_chain_exposure":\n',
        cause: {
          __class__: "SerializableErrorInfo",
          message:
            "psycopg.errors.RaiseException: entity 7989d366-e0e8-5277-bbad-ba7007e15312 was minted without its birth alias legacy-id:company:partner:apple-inc\nCONTEXT:  PL/pgSQL function staging.require_entity_birth_alias() line 9 at RAISE\n",
          cause: null,
        },
      },
    },
  },
});

/**
 * The run failure of a run that the run monitor killed, as Dagster 1.13 stores it: the event type is
 * `PIPELINE_FAILURE` (the value of `DagsterEventType.RUN_FAILURE`), no step failed and there is no error data.
 */
const RUN_FAILURE = JSON.stringify({
  __class__: "EventLogEntry",
  dagster_event: {
    __class__: "DagsterEvent",
    event_type_value: "PIPELINE_FAILURE",
    message: "Run timed out due to taking longer than 10800 seconds to complete.",
    event_specific_data: null,
  },
});

// --- firstErrorLine: the innermost cause, then the error, then the event message ---
assert(
  firstErrorLine(STEP_FAILURE) ===
    "psycopg.errors.RaiseException: entity 7989d366-e0e8-5277-bbad-ba7007e15312 was minted without its birth alias legacy-id:company:partner:apple-inc",
  "the first line of the innermost cause is the error line",
);
assert(
  firstErrorLine(RUN_FAILURE) === "Run timed out due to taking longer than 10800 seconds to complete.",
  "a run killed by the monitor has no error, so the event message is the line",
);
assert(firstErrorLine("not json") === null, "text that is not an event gives null and never throws");
assert(firstErrorLine("{}") === null, "an event without an error or a message gives null");

const WINDOW_DAYS_FOR_TEST = 7;

// --- denied: anonymous and member, the client must never be touched ---
{
  let touched = false;
  __setTestOpsClient({
    query: async () => {
      touched = true;
      throw new Error("must not query before authorization");
    },
  } as never);
  const anon = await loadPipelineOverview(null);
  const member = await loadPipelineOverview({ principalKind: "member" });
  assert(anon.kind === "denied" && member.kind === "denied", "non-administrators must be denied");
  assert(!touched, "no query may run before the deny decision");
}

type QueryCall = { sql: string; params?: unknown[] };

// --- ready: counts, rates, last failure with its error line, verdicts, release ---
{
  const calls: QueryCall[] = [];
  const recent = new Date(Date.now() - 3 * 3_600_000).toISOString();
  const older = new Date(Date.now() - 30 * 3_600_000).toISOString();
  const firstSeen = new Date(Date.now() - 50 * 3_600_000).toISOString();
  __setTestOpsClient({
    query: async (sql: string, params?: unknown[]) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      calls.push({ sql, params });
      if (sql === TIMEOUTS_SQL) {
        return { rows: [{ job: "head_reports_pipeline", count: 1 }] };
      }
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [
            {
              job: "head_reports_pipeline",
              successes: 10,
              failures: 3,
              last_success: older,
              last_failure: recent,
              last_failure_run_id: "run-failed-1",
              longest_success_seconds: 400,
            },
            {
              job: "market_data_refresh_pipeline",
              successes: 5,
              failures: 0,
              last_success: recent,
              last_failure: null,
              last_failure_run_id: null,
              longest_success_seconds: 45,
            },
            // History shows the job, the window holds no finished run of it.
            {
              job: "standard_history_backfill_pipeline",
              successes: 0,
              failures: 0,
              last_success: older,
              last_failure: null,
              last_failure_run_id: null,
              longest_success_seconds: null,
            },
          ],
        };
      }
      if (sql.includes("from dagster.event_logs")) {
        return { rows: [{ run_id: "run-failed-1", dagster_event_type: "STEP_FAILURE", event: STEP_FAILURE }] };
      }
      if (sql.includes("mart.nightly_verdicts")) {
        const check = (calls.at(-1)?.params ?? [])[0];
        return {
          rows: [
            check === "job_run_health"
              ? { ok: false, summary: "red: head_reports_pipeline (3 of 13 runs failed)", ran_at: recent }
              : { ok: null, summary: "pending", ran_at: older },
          ],
        };
      }
      if (sql === RELEASE_START_SQL) {
        assert(
          JSON.stringify(params) === JSON.stringify(["0123456789abcdef0123456789abcdef01234567"]),
          "the start of the stretch is asked for the sha of the newest run",
        );
        return { rows: [{ since: firstSeen }] };
      }
      if (sql.includes("mart.data_engine_identity")) {
        return {
          rows: [
            {
              run_id: "capture-run:e",
              git_sha: "0123456789abcdef0123456789abcdef01234567",
              image_digest: "sha256:00f7",
              created_at: recent,
            },
          ],
        };
      }
      throw new Error(`unexpected query: ${sql}`);
    },
  } as never);

  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", `administrator reads the overview, got ${outcome.kind}`);
  const { jobs, jobRunHealth, runtimeShape, release, windowDays } = outcome.data;
  assert(windowDays === WINDOW_DAYS_FOR_TEST, "the window is 7 days, like the verdict");
  assert(calls[0].sql === JOBS_SQL, "the loader sends the exported text that the real-database test runs");
  assert(calls[0].params?.[0] === WINDOW_DAYS_FOR_TEST, "the runs query is bound to the window");
  assert(calls[0].params?.[1] === 100, "a job with no finished run for 100 days is not listed (a quarterly job stays)");
  assert(calls[0].params?.[2] === 30, "the longest success query is bound to policy window days");

  const head = jobs.find((job) => job.job === "head_reports_pipeline");
  assert(head !== undefined, "a job with failures is listed");
  assert(head.runs === 13 && head.successes === 10 && head.failures === 3, "counts pass through");
  assert(Math.abs((head.successRate ?? -1) - 10 / 13) < 1e-9, "the success rate is successes over finished runs");
  assert(head.lastSuccessAt === older && head.lastFailure?.at === recent, "last success and last failure pass through");
  assert(head.lastFailure?.runId === "run-failed-1", "the last failure names its run");
  assert(
    head.lastFailure?.firstError?.startsWith("psycopg.errors.RaiseException: entity 7989d366") === true,
    "the last failure carries the first error line of its event log",
  );

  const quiet = jobs.find((job) => job.job === "market_data_refresh_pipeline");
  assert(quiet?.successRate === 1 && quiet.lastFailure === null, "a job without a failure has no last failure");

  const idle = jobs.find((job) => job.job === "standard_history_backfill_pipeline");
  assert(idle?.runs === 0 && idle.successRate === null, "no run in the window reads null, never 100%");

  const eventCall = calls.find((call) => call.sql === FAILURE_EVENTS_SQL);
  assert(eventCall?.sql === FAILURE_EVENTS_SQL, "the loader sends the exported text that the real-database test runs");
  assert(
    JSON.stringify(eventCall?.params) === JSON.stringify([["run-failed-1"]]),
    "the event log is read only for the newest failed run of each job",
  );

  const timeoutCall = calls.find((call) => call.sql === TIMEOUTS_SQL);
  assert(timeoutCall?.sql === TIMEOUTS_SQL, "the loader sends the exported TIMEOUTS_SQL text");
  assert(timeoutCall?.params?.[0] === WINDOW_DAYS_FOR_TEST, "the timeouts query is bound to the window");

  assert(head.longestSuccessSeconds === 400, "longest success duration passes through");
  assert(head.derivedBoundSeconds === 800, "derived bound is 2 * 400 = 800s");
  assert(head.runsEndedByBound === 1, "runs ended by bound passes through");

  assert(quiet?.longestSuccessSeconds === 45, "quiet job longest success is 45s");
  assert(quiet?.derivedBoundSeconds === 600, "quiet job derived bound is clamped to floor 10m = 600s");
  assert(quiet?.runsEndedByBound === 0, "quiet job has 0 runs ended by bound");

  assert(idle?.longestSuccessSeconds === null, "idle job has no longest success");
  assert(idle?.derivedBoundSeconds === null, "idle job has no derived bound");
  assert(idle?.runsEndedByBound === 0, "idle job has 0 runs ended by bound");

  assert(jobRunHealth?.ok === false && jobRunHealth.summary.startsWith("red: head_reports_pipeline"), "the verdict is shown");
  assert(runtimeShape?.ok === null, "a pending verdict stays pending, not red");
  assert(release?.gitSha.startsWith("0123456789ab") === true, "the release names the newest build");
  assert(Math.abs((release?.ageHours ?? 0) - 50) < 0.1, "the release age is the time since the start of the stretch");
}

// --- no failed run: the event log is not read ---
{
  let eventLogRead = false;
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [
            { job: "a_job", successes: 4, failures: 0, last_success: new Date().toISOString(), last_failure: null, last_failure_run_id: null },
          ],
        };
      }
      if (sql.includes("from dagster.event_logs")) eventLogRead = true;
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", "a healthy fleet is ready");
  assert(!eventLogRead, "with no failed run there is nothing to read in the event log");
  assert(outcome.data.jobRunHealth === null && outcome.data.release === null, "a missing verdict or build reads null");
}

// --- the event log cannot be read: the counts stand, the page says why the error lines are missing ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [
            {
              job: "a_job",
              successes: 3,
              failures: 1,
              last_success: "2026-10-21T01:00:00Z",
              last_failure: "2026-10-21T02:00:00Z",
              last_failure_run_id: "run-9",
            },
          ],
        };
      }
      if (sql.includes("from dagster.event_logs")) {
        throw Object.assign(new Error("permission denied for table event_logs"), { code: "42501" });
      }
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", "an unreadable event log must not hide the counts");
  assert(outcome.data.unreadable.eventLog?.includes("permission denied") === true, "the page is told why");
  assert(outcome.data.unreadable.verdicts === null && outcome.data.unreadable.release === null, "the other parts were read");
  const job = outcome.data.jobs[0];
  assert(job.failures === 1 && job.lastFailure?.runId === "run-9" && job.lastFailure.firstError === null, "counts stand");
}

// --- the verdicts or the release cannot be read: the counts stand, and the page says which part is missing ---
{
  __setTestOpsClient({
    query: async (sql: string, params?: unknown[]) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [
            { job: "a_job", successes: 3, failures: 0, last_success: "2026-10-21T01:00:00Z", last_failure: null, last_failure_run_id: null },
          ],
        };
      }
      if (sql.includes("mart.nightly_verdicts")) {
        throw Object.assign(new Error("permission denied for table nightly_verdicts"), { code: "42501" });
      }
      if (sql === RELEASE_START_SQL) throw new Error("canceling statement due to statement timeout");
      if (sql.includes("mart.data_engine_identity")) {
        return {
          rows: [{ run_id: "r", git_sha: "abc", image_digest: "sha256:1", created_at: "2026-10-21T01:00:00Z" }],
        };
      }
      void params;
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready", "an unreadable verdict table or release must not hide the counts");
  assert(outcome.data.unreadable.verdicts?.includes("permission denied") === true, "the verdicts say why they are missing");
  assert(outcome.data.unreadable.release?.includes("statement timeout") === true, "a timeout is told, never reported as no build");
  assert(outcome.data.jobs.length === 1 && outcome.data.release === null, "the counts stand and the release is missing");
}

// --- the identity view cannot be read: the page says so and does not report "no build" ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [{ job: "a_job", successes: 1, failures: 0, last_success: "2026-10-21T01:00:00Z", last_failure: null, last_failure_run_id: null }],
        };
      }
      if (sql.includes("mart.data_engine_identity")) {
        throw Object.assign(new Error("permission denied for view data_engine_identity"), { code: "42501" });
      }
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready" && outcome.data.release === null, "no release");
  assert(outcome.data.unreadable.release?.includes("permission denied") === true, "the first read failing is told, not hidden");
}

// --- the view does not exist (a database that predates it) or holds no run: no release, nothing unreadable ---
{
  for (const identity of [
    () => {
      throw Object.assign(new Error('relation "mart.data_engine_identity" does not exist'), { code: "42P01" });
    },
    () => ({ rows: [] }),
  ]) {
    __setTestOpsClient({
      query: async (sql: string) => {
        if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
        if (sql.includes("from dagster.runs")) {
          return {
            rows: [{ job: "a_job", successes: 1, failures: 0, last_success: "2026-10-21T01:00:00Z", last_failure: null, last_failure_run_id: null }],
          };
        }
        if (sql.includes("mart.data_engine_identity")) return identity();
        return { rows: [] };
      },
    } as never);
    const outcome = await loadPipelineOverview({ principalKind: "administrator" });
    assert(outcome.kind === "ready" && outcome.data.release === null && outcome.data.unreadable.release === null, "no view, no run");
  }
}

// --- the start of the stretch is null although a build exists: no age, and the page says so ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [{ job: "a_job", successes: 1, failures: 0, last_success: "2026-10-21T01:00:00Z", last_failure: null, last_failure_run_id: null }],
        };
      }
      if (sql === RELEASE_START_SQL) return { rows: [{ since: null }] };
      if (sql.includes("mart.data_engine_identity")) {
        return { rows: [{ run_id: "r", git_sha: "abc", image_digest: "sha256:1", created_at: "2026-10-21T01:00:00Z" }] };
      }
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready" && outcome.data.release === null, "null start has no age");
  assert(outcome.data.unreadable.release?.includes("no start for the build abc") === true, "and the page says so");
}

// --- a build with no recorded sha has no age, and the page says why ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      if (sql.includes("from dagster.runs")) {
        return {
          rows: [{ job: "a_job", successes: 1, failures: 0, last_success: "2026-10-21T01:00:00Z", last_failure: null, last_failure_run_id: null }],
        };
      }
      if (sql === RELEASE_START_SQL) throw new Error("must not ask for the start of an unknown build");
      if (sql.includes("mart.data_engine_identity")) {
        return {
          rows: [{ run_id: "r", git_sha: "unknown", image_digest: "unknown", created_at: "2026-10-21T01:00:00Z" }],
        };
      }
      return { rows: [] };
    },
  } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "ready" && outcome.data.release === null, "unknown build has no age");
  assert(outcome.data.unreadable.release?.includes("no build sha") === true, "and says why, not that no run exists");
}

// --- empty: Dagster has its tables and no finished run ---
{
  __setTestOpsClient({ query: async () => ({ rows: [] }) } as never);
  const outcome = await loadPipelineOverview({ principalKind: "administrator" });
  assert(outcome.kind === "empty", "no finished run reads empty, not an empty table");
}

// --- unavailable: Dagster has not created its tables; any other failure is an error ---
{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      throw Object.assign(new Error('relation "dagster.runs" does not exist'), { code: "42P01" });
    },
  } as never);
  const missing = await loadPipelineOverview({ principalKind: "administrator" });
  assert(missing.kind === "unavailable", "a missing table reads unavailable");

  __setTestOpsClient({
    query: async (sql: string) => {
      if (sql.endsWith("role app_ops_reader") || sql === "reset role") return { rows: [] };
      throw Object.assign(new Error("permission denied for table runs"), { code: "42501" });
    },
  } as never);
  const denied = await loadPipelineOverview({ principalKind: "administrator" });
  assert(
    denied.kind === "error" && denied.message.includes("permission denied"),
    "a missing grant is an error with its text, never reported as not bootstrapped",
  );
}

__setTestOpsClient(null);
console.log("#1466 pipeline loader verified");
