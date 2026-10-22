/**
 * #1466: the /admin/pipeline loader — per job, did the runs of the last 7 days succeed?
 *
 * Staging, 2026-10-20 and 2026-10-21: `topt_live_pipeline` failed on one SEC timeout and
 * `head_reports_pipeline` failed three times on one defect. The /admin overview showed the last
 * 10 runs of all jobs, which is less than one night. This loader reads `dagster.runs` once, grouped
 * by job, and then the event log of the newest failed run of each job for its first error line.
 *
 * Administrator-only, read-only SQL through the `app_ops_reader` role (db/roles.sql). The error
 * text of a Dagster event may name a host or a value. It is allowed on this page because the whole
 * /admin prefix is administrator-gated, and it never reaches `mart.nightly_verdicts` or /api/health.
 *
 * The page does not judge. The nightly verdict `job_run_health` judges (data engine,
 * `quality/job_run_health.py`) and this loader shows that verdict beside the counts.
 */

import type { PoolClient } from "pg";
import type { ReadState } from "@/server/read-state";
import { RUNS_UNAVAILABLE_MESSAGE, withOpsReader } from "./ops";

/** The window of the counts. The verdict `job_run_health` judges the same 7 days. */
export const PIPELINE_WINDOW_DAYS = 7;
/** A job whose newest finished run is older than this has left the deployment (staging held a job from July). */
export const PIPELINE_LISTED_DAYS = 30;

export interface PipelineFailure {
  runId: string;
  at: string;
  /** First line of the error of the run, or null when its event log holds none. */
  firstError: string | null;
}

export interface PipelineJobRow {
  job: string;
  /** Finished runs (SUCCESS or FAILURE) in the window. */
  runs: number;
  successes: number;
  failures: number;
  /** successes / runs, or null when the job finished no run in the window. */
  successRate: number | null;
  lastSuccessAt: string | null;
  lastFailure: PipelineFailure | null;
}

export interface PipelineVerdict {
  ok: boolean | null;
  ranAt: string;
  summary: string;
}

export interface PipelineRelease {
  gitSha: string;
  /** When a run of this build first recorded itself. */
  since: string;
  ageHours: number;
}

export interface PipelineOverview {
  windowDays: number;
  jobs: PipelineJobRow[];
  jobRunHealth: PipelineVerdict | null;
  runtimeShape: PipelineVerdict | null;
  release: PipelineRelease | null;
  appGitSha: string;
}

export type PipelineOutcome = ReadState<PipelineOverview>;

export interface PipelinePrincipal {
  principalKind: "member" | "administrator" | "service";
}

/** PostgreSQL error code of a relation that does not exist. */
const UNDEFINED_TABLE = "42P01";

/** One scan of `dagster.runs`: counts of the window, and the newest SUCCESS and FAILURE of all time. */
const JOBS_SQL = `
  select r.pipeline_name as job,
         count(*) filter (where r.status = 'SUCCESS' and r.create_timestamp >= w.since)::int as successes,
         count(*) filter (where r.status = 'FAILURE' and r.create_timestamp >= w.since)::int as failures,
         max(r.create_timestamp) filter (where r.status = 'SUCCESS') as last_success,
         max(r.create_timestamp) filter (where r.status = 'FAILURE') as last_failure,
         (array_agg(r.run_id order by r.create_timestamp desc) filter (where r.status = 'FAILURE'))[1]
           as last_failure_run_id
  from dagster.runs r
  cross join (select (now() at time zone 'utc') - make_interval(days => $1::int) as since) w
  where r.status in ('SUCCESS', 'FAILURE')
  group by r.pipeline_name
  having max(r.create_timestamp) >= (now() at time zone 'utc') - make_interval(days => $2::int)
  order by r.pipeline_name
`;

/** The first step failure of each run, or its run failure when no step failed (a run killed by the monitor). */
const FAILURE_EVENTS_SQL = `
  select distinct on (run_id) run_id, dagster_event_type, event
  from dagster.event_logs
  where run_id = any($1::text[]) and dagster_event_type in ('STEP_FAILURE', 'RUN_FAILURE')
  order by run_id, (dagster_event_type = 'STEP_FAILURE') desc, id
`;

const VERDICT_SQL = `
  select ok, summary, ran_at
  from mart.nightly_verdicts
  where check_name = $1
  order by ran_at desc, recorded_at desc
  limit 1
`;

/** The newest build, and the first time any run recorded that build. */
const RELEASE_SQL = `
  with newest as (
    select git_sha from mart.data_engine_identity order by created_at desc limit 1
  )
  select n.git_sha, (select min(i.created_at) from mart.data_engine_identity i where i.git_sha = n.git_sha) as since
  from newest n
`;

interface ErrorInfo {
  message?: unknown;
  cause?: ErrorInfo | null;
}

interface EventPayload {
  dagster_event?: {
    message?: unknown;
    event_specific_data?: { error?: ErrorInfo | null } | null;
  } | null;
}

function firstLine(text: unknown): string | null {
  if (typeof text !== "string") return null;
  const line = text.split("\n").find((candidate) => candidate.trim().length > 0);
  return line === undefined ? null : line.trim();
}

/**
 * The first error line of a serialized Dagster event: the message of the innermost `cause` of the
 * error (the exception the op raised), else the message of the error, else the message of the event.
 * Returns null for text that is not a Dagster event, never throws.
 */
export function firstErrorLine(eventJson: string): string | null {
  let payload: EventPayload;
  try {
    payload = JSON.parse(eventJson) as EventPayload;
  } catch {
    return null;
  }
  const event = payload?.dagster_event;
  let error: ErrorInfo | null | undefined = event?.event_specific_data?.error;
  let innermost: ErrorInfo | null = null;
  while (error) {
    innermost = error;
    error = error.cause;
  }
  return firstLine(innermost?.message) ?? firstLine(event?.message);
}

function iso(value: unknown): string {
  return new Date(value as string | number | Date).toISOString();
}

async function readVerdict(client: Pick<PoolClient, "query">, check: string): Promise<PipelineVerdict | null> {
  const result = await client.query(VERDICT_SQL, [check]);
  const row = result.rows[0];
  if (row === undefined) return null;
  return {
    ok: row.ok === null || row.ok === undefined ? null : Boolean(row.ok),
    ranAt: iso(row.ran_at),
    summary: row.summary === null || row.summary === undefined ? "" : String(row.summary),
  };
}

async function readRelease(client: Pick<PoolClient, "query">, now: number): Promise<PipelineRelease | null> {
  try {
    const result = await client.query(RELEASE_SQL);
    const row = result.rows[0];
    if (row === undefined || row.since === null || row.since === undefined) return null;
    const since = iso(row.since);
    return { gitSha: String(row.git_sha), since, ageHours: (now - new Date(since).getTime()) / 3_600_000 };
  } catch {
    // The view arrives with the 2026-09-07 migration; an older database reports no release.
    return null;
  }
}

async function readFailureLines(
  client: Pick<PoolClient, "query">,
  runIds: string[],
): Promise<Map<string, string | null>> {
  const lines = new Map<string, string | null>();
  if (runIds.length === 0) return lines;
  const result = await client.query(FAILURE_EVENTS_SQL, [runIds]);
  for (const row of result.rows) {
    lines.set(String(row.run_id), firstErrorLine(String(row.event)));
  }
  return lines;
}

function jobRow(row: Record<string, unknown>, lines: Map<string, string | null>): PipelineJobRow {
  const successes = Number(row.successes);
  const failures = Number(row.failures);
  const runs = successes + failures;
  const runId = row.last_failure_run_id === null || row.last_failure_run_id === undefined
    ? null
    : String(row.last_failure_run_id);
  return {
    job: String(row.job),
    runs,
    successes,
    failures,
    successRate: runs === 0 ? null : successes / runs,
    lastSuccessAt: row.last_success === null || row.last_success === undefined ? null : iso(row.last_success),
    lastFailure:
      runId === null || row.last_failure === null || row.last_failure === undefined
        ? null
        : { runId, at: iso(row.last_failure), firstError: lines.get(runId) ?? null },
  };
}

export async function loadPipelineOverview(principal: PipelinePrincipal | null): Promise<PipelineOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }
  try {
    return await withOpsReader(async (client): Promise<PipelineOutcome> => {
      let grouped;
      try {
        grouped = await client.query(JOBS_SQL, [PIPELINE_WINDOW_DAYS, PIPELINE_LISTED_DAYS]);
      } catch (error) {
        // Dagster bootstraps its own tables at runtime; a database without them reports so.
        // Any other failure (a missing grant, a timeout) is an error, never "not bootstrapped".
        if ((error as { code?: string }).code === UNDEFINED_TABLE) {
          return { kind: "unavailable", reason: RUNS_UNAVAILABLE_MESSAGE };
        }
        throw error;
      }
      if (grouped.rows.length === 0) return { kind: "empty" };
      const failedRunIds = grouped.rows
        .map((row) => row.last_failure_run_id)
        .filter((id): id is string => typeof id === "string");
      const lines = await readFailureLines(client, failedRunIds);
      return {
        kind: "ready",
        data: {
          windowDays: PIPELINE_WINDOW_DAYS,
          jobs: grouped.rows.map((row) => jobRow(row, lines)),
          jobRunHealth: await readVerdict(client, "job_run_health"),
          runtimeShape: await readVerdict(client, "runtime_shape"),
          release: await readRelease(client, Date.now()),
          appGitSha: process.env.GIT_COMMIT_SHA || "unknown",
        },
      };
    });
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
