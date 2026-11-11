/**
 * Administrator `/admin/pipeline` loader via `app_ops_reader`.
 */

import type { PoolClient } from "pg";
import { RUN_TIME_BOUNDS } from "@/contracts/environmentContract.generated";
import type { ReadState } from "@/server/read-state";
import { loadDataEngineBuild, loadReleaseStart, RUNS_UNAVAILABLE_MESSAGE, UNDEFINED_TABLE, withOpsReader } from "./ops";

export const PIPELINE_WINDOW_DAYS = 7;
export const PIPELINE_LISTED_DAYS = 100;

export interface PipelineFailure {
  runId: string;
  at: string;
  firstError: string | null;
}

export interface PipelineJobRow {
  job: string;
  runs: number;
  successes: number;
  failures: number;
  successRate: number | null;
  lastSuccessAt: string | null;
  lastFailure: PipelineFailure | null;
  longestSuccessSeconds: number | null;
  derivedBoundSeconds: number | null;
  runsEndedByBound: number;
}

export interface PipelineVerdict {
  ok: boolean | null;
  ranAt: string;
  summary: string;
}

export interface PipelineRelease {
  gitSha: string;
  since: string;
  ageHours: number;
}

export interface PipelineUnreadable {
  eventLog: string | null;
  verdicts: string | null;
  release: string | null;
}

export interface PipelineOverview {
  windowDays: number;
  unreadable: PipelineUnreadable;
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

export const JOBS_SQL = `
  select r.pipeline_name as job,
         count(*) filter (where r.status = 'SUCCESS' and r.create_timestamp >= w.since)::int as successes,
         count(*) filter (where r.status = 'FAILURE' and r.create_timestamp >= w.since)::int as failures,
         max(r.create_timestamp) filter (where r.status = 'SUCCESS') as last_success,
         max(r.create_timestamp) filter (where r.status = 'FAILURE') as last_failure,
         (array_agg(r.run_id order by r.create_timestamp desc) filter (where r.status = 'FAILURE'))[1]
           as last_failure_run_id,
         max(case when r.status = 'SUCCESS' and r.end_time is not null and r.start_time is not null and r.end_time >= r.start_time then (r.end_time - r.start_time) else null end)
           filter (where r.create_timestamp >= (now() at time zone 'utc') - make_interval(days => $3::int)) as longest_success_seconds
  from dagster.runs r
  cross join (select (now() at time zone 'utc') - make_interval(days => $1::int) as since) w
  where r.status in ('SUCCESS', 'FAILURE')
  group by r.pipeline_name
  having max(r.create_timestamp) >= (now() at time zone 'utc') - make_interval(days => $2::int)
  order by r.pipeline_name
`;

export const TIMEOUTS_SQL = `
  select r.pipeline_name as job, count(*)::int as count
  from dagster.runs r
  join dagster.event_logs e on e.run_id = r.run_id
  where r.status = 'FAILURE'
    and r.create_timestamp >= (now() at time zone 'utc') - make_interval(days => $1::int)
    and e.dagster_event_type = 'PIPELINE_FAILURE'
    and e.event like '%Run timed out due to taking longer than%'
  group by r.pipeline_name
`;

export const FAILURE_EVENTS_SQL = `
  select distinct on (run_id) run_id, dagster_event_type, event
  from dagster.event_logs
  where run_id = any($1::text[]) and dagster_event_type in ('STEP_FAILURE', 'PIPELINE_FAILURE')
  order by run_id, id
`;

const VERDICT_SQL = `
  select ok, summary, ran_at
  from mart.nightly_verdicts
  where check_name = $1
  order by ran_at desc, recorded_at desc
  limit 1
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

const iso = (v: unknown): string => new Date(v as string | number | Date).toISOString();
const errMsg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

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

async function readVerdicts(client: Pick<PoolClient, "query">) {
  try {
    return {
      jobRunHealth: await readVerdict(client, "job_run_health"),
      runtimeShape: await readVerdict(client, "runtime_shape"),
      unreadable: null,
    };
  } catch (error) {
    return { jobRunHealth: null, runtimeShape: null, unreadable: errMsg(error) };
  }
}

async function readRelease(client: Pick<PoolClient, "query">, now: number) {
  try {
    const build = await loadDataEngineBuild(client);
    if (build === null) return { release: null, unreadable: null };
    if (build.gitSha === "unknown") {
      return { release: null, unreadable: "the newest run plan records no build sha" };
    }
    const since = await loadReleaseStart(client, build.gitSha);
    if (since === null) return { release: null, unreadable: `the run plans hold no start for the build ${build.gitSha}` };
    return {
      release: { gitSha: build.gitSha, since, ageHours: (now - new Date(since).getTime()) / 3_600_000 },
      unreadable: null,
    };
  } catch (error) {
    return { release: null, unreadable: errMsg(error) };
  }
}

async function readFailureLines(client: Pick<PoolClient, "query">, runIds: string[]) {
  const lines = new Map<string, string | null>();
  if (runIds.length === 0) return { lines, unreadable: null };
  try {
    const result = await client.query(FAILURE_EVENTS_SQL, [runIds]);
    for (const row of result.rows) lines.set(String(row.run_id), firstErrorLine(String(row.event)));
    return { lines, unreadable: null };
  } catch (error) {
    return { lines, unreadable: errMsg(error) };
  }
}

async function readTimeouts(client: Pick<PoolClient, "query">, windowDays: number): Promise<Map<string, number>> {
  const counts = new Map<string, number>();
  try {
    const result = await client.query(TIMEOUTS_SQL, [windowDays]);
    for (const row of result.rows) counts.set(String(row.job), Number(row.count));
  } catch {
    // Default to 0 when event_logs is unreadable.
  }
  return counts;
}

function jobRow(
  row: Record<string, unknown>,
  lines: Map<string, string | null>,
  timeouts: Map<string, number>,
): PipelineJobRow {
  const successes = Number(row.successes);
  const failures = Number(row.failures);
  const runs = successes + failures;
  const runId = row.last_failure_run_id == null ? null : String(row.last_failure_run_id);
  const longestSuccessSeconds =
    row.longest_success_seconds == null ? null : Math.round(Number(row.longest_success_seconds));
  const derivedBoundSeconds =
    longestSuccessSeconds === null
      ? null
      : Math.max(Math.ceil(RUN_TIME_BOUNDS.margin * longestSuccessSeconds), RUN_TIME_BOUNDS.floorMinutes * 60);
  return {
    job: String(row.job),
    runs,
    successes,
    failures,
    successRate: runs === 0 ? null : successes / runs,
    lastSuccessAt: row.last_success == null ? null : iso(row.last_success),
    lastFailure:
      runId === null || row.last_failure == null
        ? null
        : { runId, at: iso(row.last_failure), firstError: lines.get(runId) ?? null },
    longestSuccessSeconds,
    derivedBoundSeconds,
    runsEndedByBound: timeouts.get(String(row.job)) ?? 0,
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
        grouped = await client.query(JOBS_SQL, [PIPELINE_WINDOW_DAYS, PIPELINE_LISTED_DAYS, RUN_TIME_BOUNDS.windowDays]);
      } catch (error) {
        if ((error as { code?: string }).code === UNDEFINED_TABLE) {
          return { kind: "unavailable", reason: RUNS_UNAVAILABLE_MESSAGE };
        }
        throw error;
      }
      if (grouped.rows.length === 0) return { kind: "empty" };
      const failedRunIds = grouped.rows
        .map((row) => row.last_failure_run_id)
        .filter((id): id is string => typeof id === "string");
      const failures = await readFailureLines(client, failedRunIds);
      const totalFailures = grouped.rows.reduce((sum, r) => sum + Number(r.failures || 0), 0);
      const timeouts = totalFailures > 0 ? await readTimeouts(client, PIPELINE_WINDOW_DAYS) : new Map<string, number>();
      const verdicts = await readVerdicts(client);
      const release = await readRelease(client, Date.now());
      return {
        kind: "ready",
        data: {
          windowDays: PIPELINE_WINDOW_DAYS,
          unreadable: { eventLog: failures.unreadable, verdicts: verdicts.unreadable, release: release.unreadable },
          jobs: grouped.rows.map((row) => jobRow(row, failures.lines, timeouts)),
          jobRunHealth: verdicts.jobRunHealth,
          runtimeShape: verdicts.runtimeShape,
          release: release.release,
          appGitSha: process.env.GIT_COMMIT_SHA || "unknown",
        },
      };
    });
  } catch (error) {
    return { kind: "error", message: errMsg(error) };
  }
}
