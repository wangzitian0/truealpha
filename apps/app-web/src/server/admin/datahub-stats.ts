/**
 * Datahub statistics for the Operate world (#641 D5).
 *
 * The init.md design gives datahub three consumption surfaces and usage views;
 * what existed were two detail pages and no statistics at all — the numbers
 * that say whether the plant is healthy (governed heads, per-factor
 * availability, source activity, capture history) lived only in psql sessions.
 * This loader is the read contract for the /admin/datahub dashboard: heads
 * with their quality grades (including the #644 factor-level availability),
 * per-source fetch activity, and recent capture runs. Read-only through
 * app_ops_reader, like every loader in this directory.
 */

import { withOpsReader } from "./ops";

export interface FactorAvailabilityRow {
  factor_id: string;
  required_semantics: string[];
  complete_subjects: number;
  universe_subjects: number;
  ratio: string;
}

export interface HeadStatusRow {
  universe_id: string;
  sequence: number;
  advanced_at: string;
  target_run_id: string;
  availability: string | null;
  agreed_cells: number | null;
  total_cells: number | null;
  factors: FactorAvailabilityRow[];
}

export interface SourceStatRow {
  source: string;
  fetches_total: number;
  fetches_24h: number;
  last_fetch: string;
}

export interface CaptureRunRow {
  run_id: string;
  universe_id: string;
  cutoff: string;
  obligations: number;
  resolved: number;
  failed: number;
  complete: boolean;
}

export interface ValidationRow {
  check: string;
  verdict: string;
  detail: string;
}

/** #729: one vendor, today (UTC), from the external call ledger — every request,
 * not only the ones that landed. `landed` counts the ok calls whose response bytes
 * exist in raw.fetches (payload_sha256 join): the traceability the owner asked for. */
export interface TrafficSourceRow {
  source: string;
  calls: number;
  failed: number;
  landed: number;
  avg_ms: number | null;
  last_call: string;
  last_error: string | null;
}

/** #729: one external request. `landed_fetch_id` is the raw.fetches row its response
 * bytes became (null for a failed call, or for a call whose bytes were not landed). */
export interface RecentCallRow {
  id: number;
  called_at: string;
  source: string;
  endpoint: string;
  caller: string;
  ok: boolean;
  status_code: number | null;
  duration_ms: number | null;
  error: string | null;
  run_key: string | null;
  landed_fetch_id: number | null;
}

export interface DatahubStats {
  heads: HeadStatusRow[];
  sources: SourceStatRow[];
  runs: CaptureRunRow[];
  validation: ValidationRow[];
  capacity: ValidationRow[];
  traffic: TrafficSourceRow[];
  recentCalls: RecentCallRow[];
}

const HEADS_SQL = `
  select h.universe_id, h.sequence, h.advanced_at::text as advanced_at, h.target_run_id,
         q.payload->>'availability' as availability,
         (select count(*)::int from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb)) c
           where c.value->>'outcome' = 'agreed') as agreed_cells,
         (select count(*)::int
            from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb))) as total_cells,
         coalesce(q.payload->'factor_availability', '{}'::jsonb) as factor_availability
  from mart.current_pointer_head h
  left join mart.datahub_quality_report q on q.run_id = h.target_run_id
  order by h.universe_id
`;

const SOURCES_SQL = `
  select source, count(*)::int as fetches_total,
         (count(*) filter (where recorded_at > now() - interval '24 hours'))::int as fetches_24h,
         max(recorded_at)::text as last_fetch
  from raw.fetches
  group by source
  order by max(recorded_at) desc, count(*) desc
  limit 12
`;

// #648/#635/#578 made the validation machinery real; this section makes it VISIBLE:
// the latest canary run (the deploy verdict), yesterday's reuse savings (vendor
// fetches avoided), the plausibility oracle's current violation count, and the
// corroborated share of the newest graded run — the basic 校验能力, one glance.
const VALIDATION_SQL = `
  select 'canary' as check,
         case when s.complete and s.failed_count = 0 then 'pass' else 'fail' end as verdict,
         'latest ' || to_char(s.cutoff, 'MM-DD HH24:MI') || ' — ' || (s.success_count + s.unchanged_count)
           || '/' || s.obligation_count || ' resolved' as detail
  from mart.topt_capture_status s
  where s.universe_id like 'universe:canary%'
  order by s.cutoff desc limit 1
`;
const REUSE_SQL = `
  select 'cross-run reuse (24h)' as check,
         case when coalesce(sum(unchanged_count), 0) > 0 then 'active' else 'idle' end as verdict,
         coalesce(sum(unchanged_count), 0) || ' vendor fetches avoided across '
           || count(*) || ' runs' as detail
  from mart.topt_capture_status where cutoff > now() - interval '24 hours'
`;
const PLAUSIBILITY_SQL = `
  select 'plausibility oracle' as check,
         case when coalesce((q.payload->>'implausible_count')::int, 0) = 0 then 'pass' else 'violations' end as verdict,
         coalesce(q.payload->>'implausible_count', '0') || ' implausible cells in the newest report' as detail
  from mart.datahub_quality_report q
  join mart.topt_capture_status s using (run_id)
  order by s.cutoff desc limit 1
`;
const CORROBORATION_SQL = `
  select 'price corroboration' as check,
         case when agreed = cells and cells > 0 then 'pass' else 'partial' end as verdict,
         agreed || '/' || cells || ' cells agreed in the newest graded run' as detail
  from (
    select (select count(*) from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb)) c
             where c.value->>'outcome' = 'agreed')::int as agreed,
           (select count(*) from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb)))::int as cells
    from mart.datahub_quality_report q
    join mart.topt_capture_status s using (run_id)
    order by s.cutoff desc limit 1
  ) newest
`;

// #671: the SP500-era budget math, watched live. Twelve Data's shared free tier
// (800/day) is the binding constraint; the reuse ratio is what keeps it flat as
// universes multiply; DB size and capture-window duration are the growth and
// runtime axes. Disk % needs a host exporter and stays on #671's ledger.
//
// #729: the credit count reads the external call ledger, not raw.fetches. Landed rows
// only ever counted the successes — every weekend tick spent two credits per listing
// on a 400 + a fallback and the old row showed 0 of 800. Failed calls spent quota too.
const CAPACITY_TD_SQL = `
  select 'twelvedata budget (this env, today utc)' as check,
         case when coalesce(sum(cost), 0) > 560 then 'warn' else 'ok' end as verdict,
         coalesce(sum(cost), 0)::int || ' of 800 shared daily credits ('
           || (count(*) filter (where not ok))::int || ' failed requests)' as detail
  from staging.api_call_ledger
  where source = 'twelvedata'
    and called_at >= (date_trunc('day', now() at time zone 'utc') at time zone 'utc')
`;

// #729: traffic, per source, today (UTC). Timestamps are rendered in UTC explicitly —
// `timestamptz::text` follows the session TimeZone (review on #741). One row per vendor; a request is counted
// whether it succeeded, answered 4xx/5xx, or raised — and `landed` says how many of the
// successful ones dereference to bytes in raw.fetches.
const TRAFFIC_SQL = `
  select l.source,
         count(*)::int as calls,
         (count(*) filter (where not l.ok))::int as failed,
         (count(*) filter (where l.ok and l.payload_sha256 is not null
            and exists (select 1 from raw.fetches f
                         where f.payload_sha256 = l.payload_sha256 and f.source = l.source)))::int as landed,
         round(avg(l.duration_ms))::int as avg_ms,
         to_char(max(l.called_at) at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS"Z"') as last_call,
         (select e.error from staging.api_call_ledger e
           where e.source = l.source and not e.ok
             and e.called_at >= (date_trunc('day', now() at time zone 'utc') at time zone 'utc')
           order by e.called_at desc limit 1) as last_error
  from staging.api_call_ledger l
  where l.called_at >= (date_trunc('day', now() at time zone 'utc') at time zone 'utc')
  group by l.source
  order by calls desc, l.source
`;

// #729: the last external requests, each traceable to the raw.fetches row its bytes
// became — or carrying the error it failed with. Both joins match the source as well as
// the digest: content addressing dedupes bytes, and two vendors could in principle
// answer identical bytes (review on #741).
const RECENT_CALLS_SQL = `
  select l.id, to_char(l.called_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS"Z"') as called_at,
         l.source, l.endpoint, l.caller, l.ok,
         l.status_code, l.duration_ms, left(l.error, 160) as error, l.run_key,
         (select min(f.id) from raw.fetches f
           where f.payload_sha256 = l.payload_sha256 and f.source = l.source) as landed_fetch_id
  from staging.api_call_ledger l
  order by l.called_at desc, l.id desc
  limit 40
`;
const CAPACITY_REUSE_SQL = `
  select 'reuse ratio (24h)' as check,
         case when coalesce(sum(obligation_count), 0) = 0 then 'idle'
              when sum(unchanged_count)::numeric / sum(obligation_count) >= 0.10 then 'ok'
              else 'low' end as verdict,
         coalesce(sum(unchanged_count), 0) || ' of ' || coalesce(sum(obligation_count), 0)
           || ' obligations satisfied without a vendor call' as detail
  from mart.topt_capture_status where cutoff > now() - interval '24 hours'
`;
const CAPACITY_DB_SQL = `
  select 'database size' as check,
         case when pg_database_size(current_database()) > 5e9 then 'warn' else 'ok' end as verdict,
         pg_size_pretty(pg_database_size(current_database())) || ' (' ||
           (select pg_size_pretty(sum(byte_length)) from raw.fetches) || ' raw bytes landed)' as detail
`;
const CAPACITY_DURATION_SQL = `
  select 'capture window (newest run per universe)' as check,
         case when max(window_minutes) > 150 then 'warn' else 'ok' end as verdict,
         string_agg(split_part(universe_id, ':', 2) || ' ' || window_minutes || 'min', ', '
                    order by universe_id) as detail
  from (
    select distinct on (s.universe_id) s.universe_id,
           coalesce(round(extract(epoch from (max(r.completed_at) - min(r.completed_at))) / 60)::int, 0)
             as window_minutes
    from mart.topt_capture_status s
    join raw.capture_obligations ob on ob.run_id = s.run_id
    join raw.capture_obligation_results r on r.capture_obligation_id = ob.obligation_id
    group by s.universe_id, s.run_id, s.cutoff
    order by s.universe_id, s.cutoff desc
  ) newest
`;

const RUNS_SQL = `
  select run_id, universe_id, cutoff::text as cutoff,
         obligation_count as obligations,
         (success_count + unchanged_count)::int as resolved,
         failed_count as failed, complete
  from mart.topt_capture_status
  order by cutoff desc
  limit 12
`;

interface FactorGrade {
  required_semantics: string[];
  complete_subjects: number;
  universe_subjects: number;
  ratio: string;
}

interface HeadDbRow extends Omit<HeadStatusRow, "factors"> {
  factor_availability: Record<string, FactorGrade>;
}

export async function loadDatahubStats(): Promise<DatahubStats> {
  return withOpsReader(async (client) => {
    const [
      heads,
      sources,
      runs,
      canary,
      reuse,
      plausibility,
      corroboration,
      td,
      reuseCap,
      dbSize,
      duration,
      traffic,
      recentCalls,
    ] = await Promise.all([
      client.query<HeadDbRow>(HEADS_SQL),
      client.query<SourceStatRow>(SOURCES_SQL),
      client.query<CaptureRunRow>(RUNS_SQL),
      client.query<ValidationRow>(VALIDATION_SQL),
      client.query<ValidationRow>(REUSE_SQL),
      client.query<ValidationRow>(PLAUSIBILITY_SQL),
      client.query<ValidationRow>(CORROBORATION_SQL),
      client.query<ValidationRow>(CAPACITY_TD_SQL),
      client.query<ValidationRow>(CAPACITY_REUSE_SQL),
      client.query<ValidationRow>(CAPACITY_DB_SQL),
      client.query<ValidationRow>(CAPACITY_DURATION_SQL),
      client.query<TrafficSourceRow>(TRAFFIC_SQL),
      client.query<RecentCallRow>(RECENT_CALLS_SQL),
    ]);
    return {
      heads: heads.rows.map((row: HeadDbRow) => ({
        universe_id: row.universe_id,
        sequence: row.sequence,
        advanced_at: row.advanced_at,
        target_run_id: row.target_run_id,
        availability: row.availability,
        agreed_cells: row.agreed_cells,
        total_cells: row.total_cells,
        factors: Object.entries(row.factor_availability ?? {}).map(([factorId, grade]) => ({
          factor_id: factorId,
          required_semantics: grade.required_semantics,
          complete_subjects: grade.complete_subjects,
          universe_subjects: grade.universe_subjects,
          ratio: grade.ratio,
        })),
      })),
      sources: sources.rows,
      runs: runs.rows,
      validation: [...canary.rows, ...reuse.rows, ...plausibility.rows, ...corroboration.rows],
      capacity: [...td.rows, ...reuseCap.rows, ...dbSize.rows, ...duration.rows],
      traffic: traffic.rows,
      recentCalls: recentCalls.rows,
    };
  });
}
