/**
 * Datahub statistics loader for `/admin/datahub` via `app_ops_reader`.
 */

import type { PoolClient } from "pg";
import {
  type EvidenceKindContract,
  MODEL_SEAT,
  UNIVERSES,
  UTILIZATION_CEILING,
  VENDOR_ACCOUNTS,
  accountShare,
  expectsVerdictFromContract,
} from "../../contracts/environmentContract.generated";
import {
  ACCEPTED_SERVICE_OBJECTIVES,
  type HeadFreshness,
  NON_CANARY_UNIVERSE_WHERE,
  REGISTERED_RAW_SOURCE_FILTER,
  UTC_DAY_START_SQL,
  isCanaryUniverse,
  resolveHeadFreshness,
} from "../../contracts/serviceObjectives";
import { idPrefixLikesOfRole } from "../../contracts/universes";
import { textOrNull } from "@/client/coerce";
import { withOpsReader } from "./ops";

type ListingRatioOutcome = "parsed" | "ordinary" | "unparsed" | "not_measured";

export interface ListingRatioCell {
  listing_id: string;
  issuer_id: string | null;
  ticker?: string | null;
  outcome: ListingRatioOutcome;
  listing_units_per_share: string | null;
  pattern: string | null;
  title: string | null;
  reason: string | null;
  accession: string | null;
  form: string | null;
  filed: string | null;
}

export interface ListingRatioReport {
  counts: Record<ListingRatioOutcome, number>;
  cells: ListingRatioCell[];
}

export interface SourceStatRow {
  source: string;
  fetches_total: number;
  fetches_24h: number;
  last_fetch: string;
}

export interface ValidationRow {
  check: string;
  verdict: string;
  detail: string;
}

export interface NightlyVerdictRow {
  check_name: string;
  ran_at: string;
  ok: boolean | null;
  summary: string;
  dagster_run_id: string | null;
}

export interface LedgerCoverageRow {
  day: string;
  source: string;
  endpoint: string;
  calls: number;
  no_key: number;
  by_reason: Record<string, number>;
  keys_twice: number;
  run_share: string;
  calls_per_run: string | null;
}

export interface QuestionCoverageQuestion {
  question: string;
  text: string;
  scope: string;
  column: string | null;
  tracking_issue: string;
  answered: number;
  unavailable_total: number;
  top_reasons: string;
  missing: number;
  denominator: number;
}

interface OracleOutcomeCell {
  compared: number;
  agree: number;
  disagree: number;
  not_comparable: number;
  no_served_value_count: number;
  newest_compared_at: string | null;
  wilson_95_upper_bound: string | null;
}

type OracleOutcomeDbRow = OracleOutcomeCell & { universe_id: string; field: string };
type VendorAccountLedgerDbRow = {
  source: string; rows_today: number; failed_today: number; http_429_today: number;
  code_1302_today: number; peak_1s: number; peak_30s: number; peak_60s: number;
};
type Model1302DayDbRow = { day: string; total_calls: number; http_429: number; code_1302: number };
type GovernedReportDbRow<Payload> = { universe_id: string; run_id: string; cutoff: string; created_at: string; payload: Payload };
type QuestionCoveragePayload = {
  generated_at?: string; requirements_sha256?: string;
  questions?: Record<string, { text?: string; scope?: string; column?: string | null; tracking_issue?: string; answered?: number; unavailable?: Record<string, number>; missing?: number; denominator?: number }>;
};
type AccuracyEntryDb = { matches_quality_report?: boolean | null; quality_report_mismatches?: string[] };
type ConfidenceReportPayload = {
  generated_at?: string; sources_connected?: string[];
  families?: Record<string, { semantic_type?: string; cells?: number; by_independent_origins?: Record<string, number>; mean_confidence?: string; compared?: number; agreement_rate?: string | null; tolerance?: string | null; origins?: string[] }>;
  accuracy?: Record<string, AccuracyEntryDb | Record<string, unknown> | undefined>;
  metadata?: { stored_confidence?: { used_for_bands?: boolean } };
};
type HeadDbRow = {
  universe_id: string; sequence: number; advanced_at: string; target_run_id: string; availability: string | null;
  agreed_cells: number | null; total_cells: number | null; freshness: HeadFreshness; age_hours: number | null; limit_hours: number | null;
  staleness_reason: string | null; environment?: string | null; head_availability?: string | null;
  factor_availability: Record<string, { required_semantics: string[]; complete_subjects: number; universe_subjects: number; ratio: string }>;
  listing_ratio?: unknown;
};
type RunDbRow = { run_id: string; universe_id: string; cutoff: string; obligations: number; resolved: number; failed: number; complete: boolean };
type TrafficDbRow = { source: string; calls: number; failed: number; landed: number; avg_ms: number | null; last_call: string; last_error: string | null };
type RecentCallDbRow = { id: number; called_at: string; source: string; endpoint: string; caller: string; ok: boolean; status_code: number | null; duration_ms: number | null; error: string | null; run_key: string | null; landed_fetch_id: number | null };

const HEAD_RUN_SQL = "coalesce(h.run_id, h.head_run_id)";
const RECON_CELLS_SQL = "jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb))";
const FETCH_MATCH_SQL = "f.payload_sha256 = l.payload_sha256 and f.source = l.source";
const LEDGER_TABLE = "staging.api_call_ledger";
const CAPTURE_TABLE = "mart.topt_capture_status";
const CAPTURE_24H_FROM = `from ${CAPTURE_TABLE} where cutoff > now() - interval '24 hours'`;
const LATEST_GRADED_FROM = `from mart.datahub_quality_report q join ${CAPTURE_TABLE} s using (run_id) where ${NON_CANARY_UNIVERSE_WHERE} order by s.cutoff desc limit 1`;
const SENTINEL_LIKES = idPrefixLikesOfRole("sentinel");
const utcTs = (col: string, alias: string, iso = false): string =>
  `to_char(${col} at time zone 'utc', '${iso ? 'YYYY-MM-DD"T"HH24:MI:SS"Z"' : 'YYYY-MM-DD HH24:MI:SS"Z"'}') as ${alias}`;
const cw = (cond: string, alias: string): string => `(count(*) filter (where ${cond}))::int as ${alias}`;
const dayWindow = (param: number): string => `>= ${UTC_DAY_START_SQL} - ($${param}::int - 1) * interval '1 day'`;
const checkSql = (name: string, verdict: string, detail: string, from: string): string =>
  `select '${name}' as check, ${verdict} as verdict, ${detail} as detail ${from}`;
const peakSql = (secs: number): string =>
  `coalesce((select max(c) from (select count(*)::int as c from today t${secs} where t${secs}.source = s.source group by bucket_${secs}s) b${secs}), 0)::int as peak_${secs}s`;

function governedLatestPerUniverseSql(table: string, extraCols = ""): string {
  return `select distinct on (universe_id) universe_id${extraCols}, cutoff::text, created_at::text, payload
from ${table} r where universe_id in (select universe_id from mart.served_head)
order by universe_id, exists (select 1 from mart.served_head h where h.universe_id = r.universe_id and coalesce(h.run_id, h.head_run_id) = r.run_id) desc, created_at desc`;
}

export const ORACLE_OUTCOME_METRICS = [
  "listed_units",
  "shares_outstanding",
  "revenue",
  "gross_profit",
  "total_assets",
  "employees_total",
  "operating_branch",
  "gross_profit_per_employee",
  "last_close",
] as const;

export const ORACLE_OUTCOMES_SQL = `with deduped as (
select distinct on (h.universe_id, o.observation_id, o.subject_id, o.field)
h.universe_id, o.field, o.result, o.reason, o.compared_at
from mart.served_head h
join mart.oracle_outcomes o on o.run_id = ${HEAD_RUN_SQL}
order by h.universe_id, o.observation_id, o.subject_id, o.field, o.compared_at desc, o.recorded_at desc, o.outcome_id desc
),
rolled as (
select universe_id, field,
${cw("result in ('agree', 'disagree')", "compared")},
${cw("result = 'agree'", "agree")},
${cw("result = 'disagree'", "disagree")},
${cw("result = 'not_comparable'", "not_comparable")},
${cw("result = 'not_comparable' and reason = 'no_served_value'", "no_served_value_count")},
${utcTs("max(compared_at)", "newest_compared_at", true)}
from deduped group by universe_id, field
)
select r.universe_id, r.field, r.compared, r.agree, r.disagree, r.not_comparable, r.no_served_value_count, r.newest_compared_at,
case when r.compared <= 0 then null
else least(1.0::numeric, greatest(0.0::numeric, round(((w.p + w.z2 / (2.0 * w.n)
+ w.z * sqrt((w.p * (1.0 - w.p) + w.z2 / (4.0 * w.n)) / w.n))
/ (1.0 + w.z2 / w.n))::numeric, 6)))::text
end as wilson_95_upper_bound
from rolled r cross join lateral (
select 1.959963984540054::numeric as z, (1.959963984540054::numeric * 1.959963984540054::numeric) as z2,
r.compared::numeric as n, case when r.compared > 0 then r.disagree::numeric / r.compared::numeric else 0.0::numeric end as p
) w order by r.universe_id, r.field`;

const VERDICT_COLS_SQL = `check_name, ${utcTs("ran_at", "ran_at")}, ok, summary, dagster_run_id`;

export const NIGHTLY_VERDICTS_SQL = `select distinct on (check_name) ${VERDICT_COLS_SQL} from mart.nightly_verdicts order by check_name, ran_at desc, recorded_at desc`;
export const NIGHTLY_VERDICT_HISTORY_SQL = `select ${VERDICT_COLS_SQL} from mart.nightly_verdicts where check_name = $1 order by ran_at desc, recorded_at desc limit 400`;

export const VENDOR_LEDGER_TODAY_SQL = `with today as (
select source, ok, status_code, provider_code,
floor(extract(epoch from called_at))::bigint as bucket_1s,
floor(extract(epoch from called_at) / 30)::bigint as bucket_30s,
floor(extract(epoch from called_at) / 60)::bigint as bucket_60s
from ${LEDGER_TABLE} where called_at >= ${UTC_DAY_START_SQL}
),
peaks as (select s.source, ${peakSql(1)}, ${peakSql(30)}, ${peakSql(60)} from (select distinct source from today) s)
select t.source, count(*)::int as rows_today,
${cw("not t.ok", "failed_today")},
${cw("t.status_code = 429", "http_429_today")},
${cw("t.provider_code = '1302'", "code_1302_today")},
p.peak_1s, p.peak_30s, p.peak_60s
from today t join peaks p on p.source = t.source
group by t.source, p.peak_1s, p.peak_30s, p.peak_60s order by t.source asc`;

export const MODEL_1302_BY_DAY_SQL = `select ((called_at at time zone 'utc')::date)::text as day,
count(*)::int as total_calls, ${cw("status_code = 429", "http_429")}, ${cw("provider_code = '1302'", "code_1302")}
from ${LEDGER_TABLE} where source = $1 and called_at ${dayWindow(2)} group by 1 order by 1 desc`;

export const LEDGER_COVERAGE_DAYS = 8;

export const LEDGER_COVERAGE_SQL = `with ledger as (
select (l.called_at at time zone 'utc')::date as day, l.source, l.endpoint, l.ok, l.fetch_key, l.run_key, l.repeat_reason
from ${LEDGER_TABLE} l where l.called_at ${dayWindow(1)}
),
per_reason as (
select day, source, endpoint, jsonb_object_agg(repeat_reason, rows) as by_reason
from (select day, source, endpoint, repeat_reason, count(*)::int as rows from ledger where repeat_reason is not null group by day, source, endpoint, repeat_reason) reasons
group by day, source, endpoint
),
twice as (
select day, source, endpoint, count(*)::int as keys_twice
from (select day, source, endpoint, fetch_key from ledger where ok and fetch_key is not null and repeat_reason is null group by day, source, endpoint, fetch_key having count(*) > 1) repeated
group by day, source, endpoint
),
totals as (
select day, source, endpoint, count(*)::int as calls, ${cw("fetch_key is null", "no_key")},
round(count(*) filter (where run_key is not null)::numeric / count(*), 4)::text as run_share,
round(count(*) filter (where run_key is not null)::numeric / nullif(count(distinct run_key), 0), 1)::text as calls_per_run
from ledger group by day, source, endpoint
)
select to_char(t.day, 'YYYY-MM-DD') as day, t.source, t.endpoint, t.calls, t.no_key,
coalesce(r.by_reason, '{}'::jsonb) as by_reason, coalesce(w.keys_twice, 0) as keys_twice, t.run_share, t.calls_per_run
from totals t left join per_reason r using (day, source, endpoint) left join twice w using (day, source, endpoint)
order by t.day desc, t.source, t.endpoint`;

const QUESTION_COVERAGE_SQL = governedLatestPerUniverseSql("mart.question_coverage_report");
const CONFIDENCE_REPORT_SQL = governedLatestPerUniverseSql("mart.datahub_confidence_report", ", run_id");

const HEADS_SQL = `select h.environment, h.universe_id, h.sequence, h.advanced_at::text as advanced_at, ${HEAD_RUN_SQL} as target_run_id,
q.payload->>'availability' as availability, h.availability as head_availability,
(select count(*)::int from ${RECON_CELLS_SQL} c where c.value->>'outcome' = 'agreed') as agreed_cells,
(select count(*)::int from ${RECON_CELLS_SQL}) as total_cells,
h.freshness, h.age_hours::float as age_hours, h.limit_hours::float as limit_hours, h.staleness_reason,
coalesce(q.payload->'factor_availability', '{}'::jsonb) as factor_availability, q.payload->'listing_ratio' as listing_ratio
from mart.served_head h left join mart.datahub_quality_report q on q.run_id = ${HEAD_RUN_SQL} order by h.universe_id`;

const SOURCES_SQL = `select source, count(*)::int as fetches_total, ${cw("recorded_at > now() - interval '24 hours'", "fetches_24h")}, max(recorded_at)::text as last_fetch
from raw.fetches where ${REGISTERED_RAW_SOURCE_FILTER} group by source order by max(recorded_at) desc, count(*) desc limit 12`;

const RUNS_SQL = `select run_id, universe_id, cutoff::text as cutoff, obligation_count as obligations,
(success_count + unchanged_count)::int as resolved, failed_count as failed, complete
from ${CAPTURE_TABLE} order by cutoff desc limit 12`;

const TRAFFIC_SQL = `select l.source, count(*)::int as calls, ${cw("not l.ok", "failed")},
${cw("l.ok and l.payload_sha256 is not null and exists (select 1 from raw.fetches f where " + FETCH_MATCH_SQL + ")", "landed")},
round(avg(l.duration_ms))::int as avg_ms, ${utcTs("max(l.called_at)", "last_call")},
(select e.error from ${LEDGER_TABLE} e where e.source = l.source and not e.ok and e.called_at >= ${UTC_DAY_START_SQL} order by e.called_at desc limit 1) as last_error
from ${LEDGER_TABLE} l where l.called_at >= ${UTC_DAY_START_SQL} group by l.source order by calls desc, l.source`;

const RECENT_CALLS_SQL = `select l.id, ${utcTs("l.called_at", "called_at")}, l.source, l.endpoint, l.caller, l.ok,
l.status_code, l.duration_ms, left(l.error, 160) as error, l.run_key,
case when l.ok and l.payload_sha256 is not null then (select min(f.id) from raw.fetches f where ${FETCH_MATCH_SQL}) end as landed_fetch_id
from ${LEDGER_TABLE} l order by l.called_at desc, l.id desc limit 40`;

const VALIDATION_QUERIES: readonly [string, unknown[]?][] = [
  [checkSql("capture obligations complete", "case when s.complete and s.failed_count = 0 then 'pass' else 'fail' end",
    "'latest ' || to_char(s.cutoff, 'MM-DD HH24:MI') || ' — ' || (s.success_count + s.unchanged_count) || '/' || s.obligation_count || ' resolved'",
    `from ${CAPTURE_TABLE} s where s.universe_id like any ($1::text[]) order by s.cutoff desc limit 1`), SENTINEL_LIKES],
  [checkSql("cross-run reuse (24h)", "case when coalesce(sum(unchanged_count), 0) > 0 then 'active' else 'idle' end",
    "coalesce(sum(unchanged_count), 0) || ' vendor fetches avoided across ' || count(*) || ' runs'", CAPTURE_24H_FROM)],
  [checkSql("plausibility sign checks", "case when coalesce((q.payload->>'implausible_count')::int, 0) = 0 then 'pass' else 'violations' end",
    "coalesce(q.payload->>'implausible_count', '0') || ' implausible cells in the newest report'", LATEST_GRADED_FROM), SENTINEL_LIKES],
  [checkSql("price corroboration", "case when agreed = cells and cells > 0 then 'pass' else 'partial' end",
    "agreed || '/' || cells || ' cells agreed in the newest graded run'",
    `from (select (select count(*) from ${RECON_CELLS_SQL} c where c.value->>'outcome' = 'agreed')::int as agreed, (select count(*) from ${RECON_CELLS_SQL})::int as cells ${LATEST_GRADED_FROM}) newest`), SENTINEL_LIKES],
];

const CAPACITY_QUERIES: readonly string[] = [
  checkSql("reuse ratio (24h)", "case when coalesce(sum(obligation_count), 0) = 0 then 'idle' when sum(unchanged_count)::numeric / sum(obligation_count) >= 0.10 then 'ok' else 'low' end",
    "coalesce(sum(unchanged_count), 0) || ' of ' || coalesce(sum(obligation_count), 0) || ' obligations satisfied without a vendor call'", CAPTURE_24H_FROM),
  checkSql("database size", "case when pg_database_size(current_database()) > 5e9 then 'warn' else 'ok' end",
    "pg_size_pretty(pg_database_size(current_database())) || ' (' || (select pg_size_pretty(sum(byte_length)) from raw.fetches) || ' raw bytes landed)'", ""),
  checkSql("capture window (newest run per universe)", "case when max(window_minutes) > 150 then 'warn' else 'ok' end",
    "string_agg(split_part(universe_id, ':', 2) || ' ' || window_minutes || 'min', ', ' order by universe_id)",
    `from (select distinct on (s.universe_id) s.universe_id, coalesce(round(extract(epoch from (max(r.completed_at) - min(r.completed_at))) / 60)::int, 0) as window_minutes from ${CAPTURE_TABLE} s join raw.capture_obligations ob on ob.run_id = s.run_id join raw.capture_obligation_results r on r.capture_obligation_id = ob.obligation_id group by s.universe_id, s.run_id, s.cutoff order by s.universe_id, s.cutoff desc) newest`),
];

const QUALITY_MATCH_ORDER = ["open", "high", "low", "close", "volume"];
const CONTRACT_ENVIRONMENTS = ["preview", "staging", "production"] as const;
const RATIO_OUTCOMES: ListingRatioOutcome[] = ["unparsed", "not_measured", "parsed", "ordinary"];
const CHECK_NAME = /^[a-z0-9_]+(@[a-z0-9_.:-]+)?$/;
const LEDGER_SUM_KEYS = ["rows_today", "failed_today", "http_429_today", "code_1302_today", "peak_1s", "peak_30s", "peak_60s"] as const;

const qualityMatchRank = (family: string) => { const idx = QUALITY_MATCH_ORDER.indexOf(family); return idx === -1 ? QUALITY_MATCH_ORDER.length : idx; };
const normalizeDashboardEnvironment = (env?: string | null) => { const raw = (env || process.env.APP_ENV || "production").trim().toLowerCase() || "production"; return raw === "prod" ? "production" : raw; };
const modelSeatLimits = (env: string) => MODEL_SEAT.environments[env] ?? null;
const shareRatio = (used: number, limit: number | null) => (limit === null ? null : limit > 0 ? used / limit : used > 0 ? Number.POSITIVE_INFINITY : 0);
const verdictRow = (r: NightlyVerdictRow): NightlyVerdictRow => ({ check_name: r.check_name, ran_at: r.ran_at, ok: r.ok == null ? null : Boolean(r.ok), summary: r.summary ?? "", dagster_run_id: r.dagster_run_id ?? null });

function questionCoverageRows(rows: GovernedReportDbRow<QuestionCoveragePayload>[]) {
  return rows.map((row) => ({
    universe_id: row.universe_id, cutoff: row.cutoff,
    generated_at: row.payload?.generated_at ?? row.created_at,
    requirements_sha256: row.payload?.requirements_sha256 ?? "",
    questions: Object.entries(row.payload?.questions ?? {}).map(([question, entry]): QuestionCoverageQuestion => {
      const unavail = entry.unavailable ?? {};
      const total = Object.values(unavail).reduce((sum, n) => sum + n, 0);
      const topReasons = Object.entries(unavail).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 3).map(([r, n]) => `${r}=${n}`).join(", ");
      return {
        question, text: entry.text ?? "", scope: entry.scope ?? (question === "q5" ? "fund" : "issuer"),
        column: entry.column ?? null, tracking_issue: entry.tracking_issue ?? "", answered: entry.answered ?? 0,
        unavailable_total: total, top_reasons: topReasons, missing: entry.missing ?? 0, denominator: entry.denominator ?? 0,
      };
    }),
  }));
}

function originCounts(byOrigins: Record<string, number> | undefined) {
  if (!byOrigins) return null;
  const counts = { none: 0, one: 0, two: 0, three_or_more: 0 };
  const keys = ["none", "one", "two"] as const;
  for (const [origins, cells] of Object.entries(byOrigins)) {
    counts[keys[Number(origins)] ?? "three_or_more"] += cells;
  }
  return counts;
}

function confidenceReportRows(rows: GovernedReportDbRow<ConfidenceReportPayload>[]) {
  return rows.map((row) => {
    const payload = row.payload ?? {};
    const accuracy = payload.accuracy ?? {};
    const qualityMatches = Object.entries(accuracy)
      .filter(([family, entry]) => family !== "sec_oracle" && entry && "matches_quality_report" in entry)
      .map(([family, entry]) => {
        const acc = entry as AccuracyEntryDb;
        return { family, matches: acc.matches_quality_report ?? null, mismatches: acc.quality_report_mismatches?.length ?? 0 };
      })
      .sort((a, b) => qualityMatchRank(a.family) - qualityMatchRank(b.family) || a.family.localeCompare(b.family));
    return {
      universe_id: row.universe_id, run_id: row.run_id, cutoff: row.cutoff,
      generated_at: payload.generated_at ?? row.created_at,
      sources_connected: payload.sources_connected ?? [],
      families: Object.entries(payload.families ?? {}).map(([family, entry]) => ({
        family, semantic_type: entry.semantic_type ?? "", cells: entry.cells ?? 0,
        origin_counts: originCounts(entry.by_independent_origins), mean_confidence: entry.mean_confidence ?? null,
        compared: entry.compared ?? 0, agreement_rate: entry.agreement_rate ?? null, tolerance: entry.tolerance ?? null, origins: entry.origins ?? [],
      })),
      close_matches_quality_report: (accuracy.close as AccuracyEntryDb | undefined)?.matches_quality_report ?? null,
      quality_report_matches: qualityMatches,
      stored_confidence_used_for_bands: payload.metadata?.stored_confidence?.used_for_bands ?? false,
    };
  });
}

export function oracleOutcomesTable(
  outcomeRows: OracleOutcomeDbRow[],
  headUniverses: string[],
  confidenceUniverses: string[] = [],
) {
  const valid = (id: string | null | undefined) => Boolean(id && !isCanaryUniverse(id));
  const universeSet = new Set([...headUniverses, ...confidenceUniverses, ...outcomeRows.map((r) => r.universe_id)].filter(valid));
  if (universeSet.size === 0) UNIVERSES.filter((u) => u.scheduled_somewhere && valid(u.id_prefix)).forEach((u) => universeSet.add(u.id_prefix));
  const universes = Array.from(universeSet).sort();
  const extra = new Set<string>();
  const byKey = new Map<string, OracleOutcomeCell>();
  for (const r of outcomeRows) {
    if (!valid(r.universe_id) || !r.field) continue;
    const compared = Number(r.compared ?? 0), notComp = Number(r.not_comparable ?? 0);
    if (compared + notComp <= 0) continue;
    if (!ORACLE_OUTCOME_METRICS.includes(r.field as (typeof ORACLE_OUTCOME_METRICS)[number])) extra.add(r.field);
    byKey.set(`${r.universe_id} ${r.field}`, {
      compared, agree: Number(r.agree ?? 0), disagree: Number(r.disagree ?? 0), not_comparable: notComp,
      no_served_value_count: Number(r.no_served_value_count ?? 0), newest_compared_at: r.newest_compared_at ?? null,
      wilson_95_upper_bound: r.wilson_95_upper_bound ?? null,
    });
  }
  return {
    universes,
    rows: [...ORACLE_OUTCOME_METRICS, ...Array.from(extra).sort()].map((metric) => ({
      metric, cells: Object.fromEntries(universes.map((u) => [u, byKey.get(`${u} ${metric}`) ?? null])),
    })),
  };
}

export async function loadVerdictHistory(
  check: string,
  principal: { principalKind: "member" | "administrator" | "service" } | null,
): Promise<NightlyVerdictRow[] | null> {
  if (!principal || principal.principalKind !== "administrator" || !CHECK_NAME.test(check)) return null;
  return withOpsReader(async (client) => (await client.query<NightlyVerdictRow>(NIGHTLY_VERDICT_HISTORY_SQL, [check])).rows.map(verdictRow));
}

export function vendorAccountRows(dbRows: readonly Partial<VendorAccountLedgerDbRow>[], environment?: string | null) {
  const env = normalizeDashboardEnvironment(environment);
  const bySource = new Map(dbRows.filter((r): r is VendorAccountLedgerDbRow => typeof r.source === "string" && r.source.length > 0 && typeof r.rows_today === "number").map((r) => [r.source, r]));

  return VENDOR_ACCOUNTS.map((account) => {
    const share = accountShare(account.name, env);
    const sharesByEnvironment = Object.fromEntries(CONTRACT_ENVIRONMENTS.map((e) => [e, { environment: e, percent: accountShare(account.name, e).percent }]));
    const sums = Object.fromEntries(
      LEDGER_SUM_KEYS.map((k) => [k, account.seats.reduce((sum, seat) => sum + Number(bySource.get(seat)?.[k] ?? 0), 0)]),
    ) as Record<(typeof LEDGER_SUM_KEYS)[number], number>;

    const limits = account.name === "model_plan" ? modelSeatLimits(env) : null;
    const rateWindowSeconds = share.rateWindowSeconds ?? (limits ? 60 : null);
    const rateWindowShare = share.rateWindowShare ?? limits?.calls_per_minute ?? null;
    const peakCalls = (rateWindowSeconds === 1 || rateWindowSeconds === 30 || rateWindowSeconds === 60) ? sums[`peak_${rateWindowSeconds}s`] : 0;
    const ratio = shareRatio(sums.rows_today, share.dailyShare);
    const dailyRatioText = ratio === null ? null : Number.isFinite(ratio) ? ratio.toFixed(2) : "inf";
    const dailyUsageText = share.dailyShare !== null ? `${sums.rows_today} of ${share.dailyShare}` : `${sums.rows_today} rows (no daily limit)`;
    const windowUsageText = rateWindowShare !== null && rateWindowSeconds !== null ? `${peakCalls} of ${rateWindowShare} per ${rateWindowSeconds}s` : `${peakCalls} calls (no rate figure)`;
    const evidenceKinds: EvidenceKindContract[] = Array.from(new Set(share.limits.map((l) => l.evidence.kind)));
    const notKnownLimits = share.limits.filter((l) => l.evidence.kind === "not_known" || l.evidence.kind === "none_published").map((l) => ({ id: l.limitId, evidence_kind: l.evidence.kind }));
    const overDaily = share.dailyShare !== null && (share.dailyShare === 0 ? sums.rows_today > 0 : sums.rows_today / share.dailyShare > UTILIZATION_CEILING);
    const overWindow = rateWindowShare !== null && (rateWindowShare === 0 ? peakCalls > 0 : peakCalls > rateWindowShare);
    const verdict: "ok" | "warn" = overDaily || overWindow || sums.http_429_today > 0 || sums.code_1302_today > 0 ? "warn" : "ok";
    const dailyDetail = share.dailyShare !== null ? `${sums.rows_today} of ${share.dailyShare} daily share (ratio ${dailyRatioText}, ceiling ${UTILIZATION_CEILING.toFixed(2)}, ${sums.failed_today} failed)` : `${sums.rows_today} rows today (no daily limit, ${sums.failed_today} failed)`;
    const unmeasured = notKnownLimits.length > 0 ? `; unmeasured=${notKnownLimits.map((l) => `${l.id}:${l.evidence_kind}`).join(",")}` : "";

    return {
      account: account.name, holder: account.holder, seats: account.seats, environment: env, percent: share.percent,
      rows_today: sums.rows_today, daily_share: share.dailyShare, daily_ratio_text: dailyRatioText, daily_usage_text: dailyUsageText,
      peak_window_calls: peakCalls, rate_window_seconds: rateWindowSeconds, rate_window_share: rateWindowShare, window_usage_text: windowUsageText,
      http_429_today: sums.http_429_today, code_1302_today: sums.code_1302_today, evidence_kinds: evidenceKinds, not_known_limits: notKnownLimits,
      shares_by_environment: sharesByEnvironment, verdict, detail: `${dailyDetail}; peak ${windowUsageText}; 429=${sums.http_429_today}, 1302=${sums.code_1302_today}; evidence=${evidenceKinds.join(",")}${unmeasured}`,
    };
  });
}

export function vendorAccountCapacityRows(rows: ReturnType<typeof vendorAccountRows>): ValidationRow[] {
  return rows.map((row) => ({
    check: `${row.account} (${row.seats.join(", ")}, ${row.environment}, today utc)`,
    verdict: row.verdict,
    detail: row.detail,
  }));
}

export function model1302Rows(dbRows: readonly Partial<Model1302DayDbRow>[], environment?: string | null) {
  const env = normalizeDashboardEnvironment(environment);
  const limits = modelSeatLimits(env);
  return dbRows
    .filter((row): row is Model1302DayDbRow => typeof row.day === "string" && row.day.length > 0)
    .map((row) => ({
      environment: env,
      day: row.day,
      total_calls: Number(row.total_calls ?? 0),
      http_429: Number(row.http_429 ?? 0),
      code_1302: Number(row.code_1302 ?? 0),
      calls_per_minute_limit: limits?.calls_per_minute ?? null,
      max_concurrent_calls: limits?.max_concurrent_calls ?? null,
    }));
}

export function listingRatioReport(block: unknown): ListingRatioReport | null {
  if (block === null || typeof block !== "object") return null;
  const { counts, cells } = block as { counts?: Record<string, unknown>; cells?: Record<string, unknown> };
  if (!counts || !cells) return null;
  const rows: ListingRatioCell[] = [];
  for (const [key, raw] of Object.entries(cells)) {
    const entry = raw as Record<string, unknown>;
    const outcome = RATIO_OUTCOMES.find((c) => c === entry.outcome);
    if (!outcome) continue;
    const t = (k: string) => textOrNull(entry[k]);
    rows.push({
      listing_id: t("listing_id") ?? key, issuer_id: t("issuer_id"), ticker: t("ticker"), outcome,
      listing_units_per_share: t("listing_units_per_share"), pattern: t("pattern"), title: t("title"),
      reason: t("reason"), accession: t("accession"), form: t("form"), filed: t("filed"),
    });
  }
  rows.sort((a, b) => RATIO_OUTCOMES.indexOf(a.outcome) - RATIO_OUTCOMES.indexOf(b.outcome) || a.listing_id.localeCompare(b.listing_id));
  return {
    counts: { parsed: Number(counts.parsed ?? 0), ordinary: Number(counts.ordinary ?? 0), unparsed: Number(counts.unparsed ?? 0), not_measured: Number(counts.not_measured ?? 0) },
    cells: rows,
  };
}

function toHeadStatusRow(row: HeadDbRow) {
  const { minimum_availability: minAvail, minimum_corroborated_share: minCorrob } = ACCEPTED_SERVICE_OBJECTIVES;
  const pct = (ratio: number, digits: number) => `${(ratio * 100).toFixed(digits)}%`;
  const parsedAvail = row.availability != null ? parseFloat(row.availability) : Number.NaN;
  const availNum = Number.isNaN(parsedAvail) ? null : parsedAvail;
  const headWithheld = row.head_availability !== undefined ? row.head_availability !== "available" : availNum === null && row.availability !== "available";
  const isAvailDegraded = (availNum !== null && availNum < minAvail) || headWithheld;
  const corrobRatio = row.total_cells !== null && row.total_cells > 0 && row.agreed_cells !== null ? row.agreed_cells / row.total_cells : null;
  const isCorrobDegraded = !isCanaryUniverse(row.universe_id) && corrobRatio !== null && corrobRatio < minCorrob;
  const freshness = resolveHeadFreshness(row.freshness, row.universe_id, row.environment);

  const degraded_reasons: string[] = [];
  if (isAvailDegraded) {
    degraded_reasons.push(
      availNum !== null && availNum < minAvail
        ? `availability (${pct(availNum, 1)} < ${pct(minAvail, 0)})`
        : `availability (${[row.head_availability, row.availability].find((v) => v && v !== "available") ?? "unavailable"})`,
    );
  }
  if (isCorrobDegraded && corrobRatio !== null) {
    degraded_reasons.push(`corroboration (${pct(corrobRatio, 1)} < ${pct(minCorrob, 0)})`);
  }
  if (freshness !== "fresh" && freshness !== "manual") {
    degraded_reasons.push(`freshness (${row.staleness_reason ? `${freshness}: ${row.staleness_reason}` : freshness})`);
  }

  return {
    universe_id: row.universe_id, sequence: row.sequence, advanced_at: row.advanced_at, target_run_id: row.target_run_id,
    availability: row.availability, agreed_cells: row.agreed_cells, total_cells: row.total_cells, freshness,
    age_hours: row.age_hours != null ? Number(row.age_hours) : null, limit_hours: row.limit_hours != null ? Number(row.limit_hours) : null,
    staleness_reason: row.staleness_reason ?? null, is_degraded: degraded_reasons.length > 0, is_avail_degraded: isAvailDegraded,
    is_corroboration_degraded: isCorrobDegraded, degraded_reasons,
    factors: Object.entries(row.factor_availability ?? {}).map(([factor_id, grade]) => ({ factor_id, ...grade })),
    listing_ratio: listingRatioReport(row.listing_ratio),
  };
}

async function queryRows<Row>(client: Pick<PoolClient, "query">, sql: string, params?: unknown[]): Promise<Row[]> {
  return (await client.query(sql, params)).rows as Row[];
}

export async function loadDatahubStats() {
  return withOpsReader(async (client) => {
    const q = <T>(sql: string, p?: unknown[]) => queryRows<T>(client, sql, p);
    const [
      headRows, sources, runs, validationGroups, vendorLedgerToday, model1302Daily,
      capacityGroups, traffic, recentCalls, ledgerCoverage, questionCoverage,
      confidence, oracleOutcomes, nightlyVerdicts,
    ] = await Promise.all([
      q<HeadDbRow>(HEADS_SQL), q<SourceStatRow>(SOURCES_SQL), q<RunDbRow>(RUNS_SQL),
      Promise.all(VALIDATION_QUERIES.map(([sql, p]) => q<ValidationRow>(sql, p ? [p] : undefined))),
      q<VendorAccountLedgerDbRow>(VENDOR_LEDGER_TODAY_SQL),
      q<Model1302DayDbRow>(MODEL_1302_BY_DAY_SQL, [MODEL_SEAT.seat, LEDGER_COVERAGE_DAYS]),
      Promise.all(CAPACITY_QUERIES.map((sql) => q<ValidationRow>(sql))),
      q<TrafficDbRow>(TRAFFIC_SQL), q<RecentCallDbRow>(RECENT_CALLS_SQL),
      q<LedgerCoverageRow>(LEDGER_COVERAGE_SQL, [LEDGER_COVERAGE_DAYS]),
      q<GovernedReportDbRow<QuestionCoveragePayload>>(QUESTION_COVERAGE_SQL),
      q<GovernedReportDbRow<ConfidenceReportPayload>>(CONFIDENCE_REPORT_SQL),
      q<OracleOutcomeDbRow>(ORACLE_OUTCOMES_SQL), q<NightlyVerdictRow>(NIGHTLY_VERDICTS_SQL),
    ]);

    const activeEnv = process.env.APP_ENV || headRows.find((r) => r.environment)?.environment || "production";
    const vendorAccounts = vendorAccountRows(vendorLedgerToday, activeEnv);
    return {
      oracleOutcomes: oracleOutcomesTable(oracleOutcomes, headRows.map((r) => r.universe_id), confidence.map((r) => r.universe_id)),
      confidence: confidenceReportRows(confidence),
      questionCoverage: questionCoverageRows(questionCoverage),
      nightlyVerdicts: nightlyVerdicts.filter((r) => expectsVerdictFromContract(r.check_name, activeEnv)).map(verdictRow),
      heads: headRows.map(toHeadStatusRow), sources, runs, validation: validationGroups.flat(),
      capacity: [...vendorAccountCapacityRows(vendorAccounts), ...capacityGroups.flat()],
      vendorAccounts, model1302ByDay: model1302Rows(model1302Daily, activeEnv), traffic, recentCalls, ledgerCoverage,
    };
  });
}
