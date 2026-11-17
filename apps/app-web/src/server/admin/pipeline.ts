/**
 * Administrator `/admin/pipeline` loader via `app_ops_reader`.
 */

import { RUN_TIME_BOUNDS } from "@/contracts/environmentContract.generated";
import type { ReadState } from "@/server/read-state";
import {
  errMessage,
  isUndefinedTable,
  loadDataEngineBuild,
  loadReleaseStart,
  RUNS_UNAVAILABLE_MESSAGE,
  runAdminRead,
  withOpsReader,
  type OpsPrincipal,
  type OpsQueryClient,
} from "./ops";

export const PIPELINE_WINDOW_DAYS = 7;
export const PIPELINE_LISTED_DAYS = 100;

export type PipelineFailure = { runId: string; at: string; firstError: string | null };
export type PipelineJobRow = {
  job: string; runs: number; successes: number; failures: number; successRate: number | null;
  lastSuccessAt: string | null; lastFailure: PipelineFailure | null; longestSuccessSeconds: number | null;
  derivedBoundSeconds: number | null; runsEndedByBound: number;
};
export type PipelineVerdict = { ok: boolean | null; ranAt: string; summary: string };
export type PipelineRelease = { gitSha: string; since: string; ageHours: number };
export type PipelineUnreadable = { eventLog: string | null; verdicts: string | null; release: string | null };
export type PipelineOverview = {
  windowDays: number; unreadable: PipelineUnreadable; jobs: PipelineJobRow[];
  jobRunHealth: PipelineVerdict | null; release: PipelineRelease | null; appGitSha: string;
};

export type PipelineOutcome = ReadState<PipelineOverview>;
export type PipelinePrincipal = OpsPrincipal;

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
  return text.split("\n").find((line) => line.trim().length > 0)?.trim() ?? null;
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

async function tryReadPart<T>(fallback: T, fn: () => Promise<{ value: T; reason?: string | null }>): Promise<{ value: T; unreadable: string | null }> {
  try {
    const out = await fn();
    return { value: out.value, unreadable: out.reason ?? null };
  } catch (error) {
    return { value: fallback, unreadable: errMessage(error) };
  }
}

async function readVerdictPart(client: OpsQueryClient) {
  return tryReadPart<PipelineVerdict | null>(null, async () => {
    const row = (await client.query(VERDICT_SQL, ["job_run_health"])).rows[0];
    return {
      value: row
        ? {
            ok: row.ok == null ? null : Boolean(row.ok),
            ranAt: iso(row.ran_at),
            summary: row.summary == null ? "" : String(row.summary),
          }
        : null,
    };
  });
}

async function readReleasePart(client: OpsQueryClient, now: number) {
  return tryReadPart<PipelineRelease | null>(null, async () => {
    const build = await loadDataEngineBuild(client);
    if (build === null) return { value: null };
    if (build.gitSha === "unknown") return { value: null, reason: "the newest run plan records no build sha" };
    const since = await loadReleaseStart(client, build.gitSha);
    if (since === null) return { value: null, reason: `the run plans hold no start for the build ${build.gitSha}` };
    return { value: { gitSha: build.gitSha, since, ageHours: (now - new Date(since).getTime()) / 3_600_000 } };
  });
}

async function readFailureLinesPart(client: OpsQueryClient, runIds: string[]) {
  const lines = new Map<string, string | null>();
  if (runIds.length === 0) return { value: lines, unreadable: null };
  return tryReadPart(lines, async () => {
    const result = await client.query(FAILURE_EVENTS_SQL, [runIds]);
    for (const row of result.rows) lines.set(String(row.run_id), firstErrorLine(String(row.event)));
    return { value: lines };
  });
}

async function readTimeouts(client: OpsQueryClient, windowDays: number): Promise<Map<string, number>> {
  const counts = new Map<string, number>();
  try {
    const result = await client.query(TIMEOUTS_SQL, [windowDays]);
    for (const row of result.rows) counts.set(String(row.job), Number(row.count));
  } catch {
    // Default to 0 when event_logs is unreadable.
  }
  return counts;
}

function jobRow(row: Record<string, unknown>, lines: Map<string, string | null>, timeouts: Map<string, number>): PipelineJobRow {
  const successes = Number(row.successes);
  const failures = Number(row.failures);
  const runs = successes + failures;
  const runId = row.last_failure_run_id == null ? null : String(row.last_failure_run_id);
  const longestSuccessSeconds = row.longest_success_seconds == null ? null : Math.round(Number(row.longest_success_seconds));
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
    lastFailure: runId === null || row.last_failure == null ? null : { runId, at: iso(row.last_failure), firstError: lines.get(runId) ?? null },
    longestSuccessSeconds,
    derivedBoundSeconds,
    runsEndedByBound: timeouts.get(String(row.job)) ?? 0,
  };
}

export async function loadPipelineOverview(principal: PipelinePrincipal | null): Promise<PipelineOutcome> {
  return runAdminRead(principal, () =>
    withOpsReader(async (client): Promise<PipelineOutcome> => {
      let grouped;
      try {
        grouped = await client.query(JOBS_SQL, [PIPELINE_WINDOW_DAYS, PIPELINE_LISTED_DAYS, RUN_TIME_BOUNDS.windowDays]);
      } catch (error) {
        if (isUndefinedTable(error)) return { kind: "unavailable", reason: RUNS_UNAVAILABLE_MESSAGE };
        throw error;
      }
      if (grouped.rows.length === 0) return { kind: "empty" };
      const failedRunIds = grouped.rows.map((row) => row.last_failure_run_id).filter((id): id is string => typeof id === "string");
      const failures = await readFailureLinesPart(client, failedRunIds);
      const totalFailures = grouped.rows.reduce((sum, r) => sum + Number(r.failures || 0), 0);
      const timeouts = totalFailures > 0 ? await readTimeouts(client, PIPELINE_WINDOW_DAYS) : new Map<string, number>();
      const verdicts = await readVerdictPart(client);
      const release = await readReleasePart(client, Date.now());
      return {
        kind: "ready",
        data: {
          windowDays: PIPELINE_WINDOW_DAYS,
          unreadable: { eventLog: failures.unreadable, verdicts: verdicts.unreadable, release: release.unreadable },
          jobs: grouped.rows.map((row) => jobRow(row, failures.value, timeouts)),
          jobRunHealth: verdicts.value,
          release: release.value,
          appGitSha: process.env.GIT_COMMIT_SHA || "unknown",
        },
      };
    }),
  );
}
