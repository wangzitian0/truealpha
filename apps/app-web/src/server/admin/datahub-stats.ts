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

import {
  type EvidenceKindContract,
  MODEL_SEAT,
  UNIVERSES,
  UTILIZATION_CEILING,
  VENDOR_ACCOUNTS,
  accountShare,
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
import { withOpsReader } from "./ops";

export interface FactorAvailabilityRow {
  factor_id: string;
  required_semantics: string[];
  complete_subjects: number;
  universe_subjects: number;
  ratio: string;
}

/**
 * #1519 step 1a: how the cover of each issuer's latest annual filing read for its listing. `parsed` is a depositary
 * ratio, `ordinary` is a plain share (ratio 1), `unparsed` keeps the title text and a reason, and `not_measured` is
 * a payload that has no ratio key. The data engine writes the block into the quality report; the App only formats it.
 */
export type ListingRatioOutcome = "parsed" | "ordinary" | "unparsed" | "not_measured";

export interface ListingRatioCell {
  listing_id: string;
  issuer_id: string | null;
  outcome: ListingRatioOutcome;
  /** Ordinary shares that one listed unit stands for. */
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
  /** The listings that need a look come first: `unparsed`, `not_measured`, `parsed`, `ordinary`. */
  cells: ListingRatioCell[];
}

export interface HeadStatusRow {
  universe_id: string;
  sequence: number;
  advanced_at: string;
  target_run_id: string;
  availability: string | null;
  agreed_cells: number | null;
  total_cells: number | null;
  freshness: HeadFreshness;
  age_hours: number | null;
  limit_hours: number | null;
  staleness_reason: string | null;
  is_degraded: boolean;
  is_avail_degraded: boolean;
  is_corroboration_degraded: boolean;
  degraded_reasons: string[];
  factors: FactorAvailabilityRow[];
  /** null: the quality report of the target run was written before the ratio block existed. */
  listing_ratio: ListingRatioReport | null;
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

/** #1346 / #1227: In-environment standing check verdicts from `mart.nightly_verdicts`. */
export interface NightlyVerdictRow {
  check_name: string;
  ran_at: string;
  /** null is a pending verdict: the check ran and could not judge yet. It is neither ok nor a failure. */
  ok: boolean | null;
  summary: string;
  dagster_run_id: string | null;
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

/**
 * #1521 step 6: what the ledger says about fetch keys and runs, per UTC day, vendor and endpoint. The table replaced
 * the nightly fetch-key verdict: the numbers are on the page and no check turns red on them. SQL computes every
 * count and ratio; the App formats them.
 */
export interface LedgerCoverageRow {
  /** The UTC day, `YYYY-MM-DD`. */
  day: string;
  source: string;
  endpoint: string;
  calls: number;
  /** Rows whose `fetch_key` is null. Rows written before #1521 step 2 stay in this count. */
  no_key: number;
  /** Rows per `repeat_reason`. The gateway derives the reason; a repeat with a reason is no duplicate fetch. */
  by_reason: Record<string, number>;
  /** Keys with two or more successful rows and no reason on this day. A failed retry never counts. */
  keys_twice: number;
  /** The share of rows that name a run, a fraction from 0 to 1 as text. */
  run_share: string;
  /** The rows that name a run, divided by the distinct runs; null when no row names a run. */
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

export interface QuestionCoverageRow {
  universe_id: string;
  cutoff: string;
  generated_at: string;
  requirements_sha256: string;
  questions: QuestionCoverageQuestion[];
}

interface QuestionCoverageDbRow {
  universe_id: string;
  cutoff: string;
  created_at: string;
  payload: {
    generated_at?: string;
    requirements_sha256?: string;
    questions?: Record<
      string,
      {
        text?: string;
        scope?: string;
        column?: string | null;
        tracking_issue?: string;
        answered?: number;
        unavailable?: Record<string, number>;
        missing?: number;
        denominator?: number;
      }
    >;
  };
}

/** #748 / #1359 / #1362: the newest weekly question-coverage report per active governed universe. */
const QUESTION_COVERAGE_SQL = `
  select distinct on (universe_id) universe_id, cutoff::text, created_at::text, payload
  from mart.question_coverage_report
  where universe_id in (select universe_id from mart.served_head)
  order by universe_id, created_at desc
`;

function questionCoverageRows(
  rows: QuestionCoverageDbRow[],
): QuestionCoverageRow[] {
  return rows.map((row) => {
    const payload = row.payload ?? {};
    return {
      universe_id: row.universe_id,
      cutoff: row.cutoff,
      generated_at: payload.generated_at ?? row.created_at,
      requirements_sha256: payload.requirements_sha256 ?? "",
      questions: Object.entries(payload.questions ?? {}).map(
        ([question, entry]) => {
          const unavailable = entry.unavailable ?? {};
          const total = Object.values(unavailable).reduce(
            (sum, n) => sum + n,
            0,
          );
          const topReasons = Object.entries(unavailable)
            .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
            .slice(0, 3)
            .map(([reason, n]) => `${reason}=${n}`)
            .join(", ");
          return {
            question,
            text: entry.text ?? "",
            scope: entry.scope ?? (question === "q5" ? "fund" : "issuer"),
            column: entry.column ?? null,
            tracking_issue: entry.tracking_issue ?? "",
            answered: entry.answered ?? 0,
            unavailable_total: total,
            top_reasons: topReasons,
            missing: entry.missing ?? 0,
            denominator: entry.denominator ?? 0,
          };
        },
      ),
    };
  });
}

/** One family's cross-check against the persisted quality report (`accuracy.<family>`). */
export interface ConfidenceQualityMatch {
  family: string;
  matches: boolean | null;
  mismatches: number;
}

/** The bar's own order for the price families; every other cross-checked family follows
 *  alphabetically. The payload is `jsonb`, which does not keep key order, so the order is
 *  imposed here rather than read. */
const QUALITY_MATCH_ORDER = ["open", "high", "low", "close", "volume"];

function qualityMatchRank(family: string): number {
  const index = QUALITY_MATCH_ORDER.indexOf(family);
  return index === -1 ? QUALITY_MATCH_ORDER.length : index;
}

interface AccuracyEntryDb {
  matches_quality_report?: boolean | null;
  quality_report_mismatches?: string[];
}

/**
 * #1523 S3: one `(metric, universe)` cell of the `mart.oracle_outcomes` rollup table on `/admin/datahub`.
 * SQL computes the deduplicated counts, the newest comparison timestamp, and the 95% Wilson upper bound on
 * the disagreement rate; the App only formats the cell or renders `no comparator` when null.
 */
export interface OracleOutcomeCell {
  compared: number;
  agree: number;
  disagree: number;
  not_comparable: number;
  newest_compared_at: string | null;
  wilson_95_upper_bound: string | null;
}

export interface OracleOutcomeMetricRow {
  metric: string;
  cells: Record<string, OracleOutcomeCell | null>;
}

export interface OracleOutcomesTable {
  universes: string[];
  rows: OracleOutcomeMetricRow[];
}

export interface OracleOutcomeDbRow {
  universe_id: string;
  field: string;
  compared: number;
  agree: number;
  disagree: number;
  not_comparable: number;
  newest_compared_at: string | null;
  wilson_95_upper_bound: string | null;
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

/**
 * #1523 S3: per-universe, per-metric rollup of `mart.oracle_outcomes` for the current governed heads in
 * `mart.served_head`. Deduplicates by `(universe_id, observation_id, subject_id, field)` with newest outcome
 * winning, matching `data_engine.datahub.oracle_outcomes.rollup_oracle_outcomes`.
 */
export const ORACLE_OUTCOMES_SQL = `
  with deduped as (
    select distinct on (h.universe_id, o.observation_id, o.subject_id, o.field)
      h.universe_id,
      o.field,
      o.result,
      o.compared_at
    from mart.served_head h
    join mart.oracle_outcomes o
      on o.run_id = coalesce(h.run_id, h.head_run_id)
    order by h.universe_id, o.observation_id, o.subject_id, o.field, o.compared_at desc, o.recorded_at desc, o.outcome_id desc
  ),
  rolled as (
    select
      universe_id,
      field,
      (count(*) filter (where result in ('agree', 'disagree')))::int as compared,
      (count(*) filter (where result = 'agree'))::int as agree,
      (count(*) filter (where result = 'disagree'))::int as disagree,
      (count(*) filter (where result = 'not_comparable'))::int as not_comparable,
      to_char(max(compared_at) at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as newest_compared_at
    from deduped
    group by universe_id, field
  )
  select
    universe_id,
    field,
    compared,
    agree,
    disagree,
    not_comparable,
    newest_compared_at,
    case
      when compared <= 0 then null
      else least(
        1.0::numeric,
        greatest(
          0.0::numeric,
          round(
            (
              (
                (disagree::numeric / compared::numeric)
                + (1.959963984540054::numeric * 1.959963984540054::numeric) / (2.0::numeric * compared::numeric)
                + 1.959963984540054::numeric * sqrt(
                  (
                    (disagree::numeric / compared::numeric) * (1.0::numeric - (disagree::numeric / compared::numeric))
                    + (1.959963984540054::numeric * 1.959963984540054::numeric) / (4.0::numeric * compared::numeric)
                  ) / compared::numeric
                )
              ) / (1.0::numeric + (1.959963984540054::numeric * 1.959963984540054::numeric) / compared::numeric)
            )::numeric,
            6
          )
        )
      )::text
    end as wilson_95_upper_bound
  from rolled
  order by universe_id, field
`;

export function oracleOutcomesTable(
  outcomeRows: OracleOutcomeDbRow[],
  headUniverses: string[],
  confidenceUniverses: string[] = [],
): OracleOutcomesTable {
  const universeSet = new Set<string>();
  for (const universeId of [...headUniverses, ...confidenceUniverses, ...outcomeRows.map((row) => row.universe_id)]) {
    if (universeId && !isCanaryUniverse(universeId)) {
      universeSet.add(universeId);
    }
  }
  if (universeSet.size === 0) {
    for (const universe of UNIVERSES) {
      if (universe.scheduled_somewhere && !isCanaryUniverse(universe.id_prefix)) {
        universeSet.add(universe.id_prefix);
      }
    }
  }
  const universes = Array.from(universeSet).sort((a, b) => a.localeCompare(b));

  const extraMetrics = new Set<string>();
  const byUniverseAndField = new Map<string, OracleOutcomeCell>();
  for (const row of outcomeRows) {
    if (!row.universe_id || isCanaryUniverse(row.universe_id) || !row.field) continue;
    const compared = Number(row.compared ?? 0);
    const agree = Number(row.agree ?? 0);
    const disagree = Number(row.disagree ?? 0);
    const notComparable = Number(row.not_comparable ?? 0);
    if (compared + notComparable <= 0) continue;
    if (!ORACLE_OUTCOME_METRICS.includes(row.field as (typeof ORACLE_OUTCOME_METRICS)[number])) {
      extraMetrics.add(row.field);
    }
    byUniverseAndField.set(`${row.universe_id}\0${row.field}`, {
      compared,
      agree,
      disagree,
      not_comparable: notComparable,
      newest_compared_at: row.newest_compared_at ?? null,
      wilson_95_upper_bound: row.wilson_95_upper_bound ?? null,
    });
  }

  const metrics = [
    ...ORACLE_OUTCOME_METRICS,
    ...Array.from(extraMetrics).sort((a, b) => a.localeCompare(b)),
  ];

  return {
    universes,
    rows: metrics.map((metric) => {
      const cells: Record<string, OracleOutcomeCell | null> = {};
      for (const universeId of universes) {
        cells[universeId] = byUniverseAndField.get(`${universeId}\0${metric}`) ?? null;
      }
      return { metric, cells };
    }),
  };
}

/** Cells of one family by the number of independent origins that asserted a value. */
export interface ConfidenceOriginCounts {
  none: number;
  one: number;
  two: number;
  three_or_more: number;
}

/** The nightly confidence & accuracy report: one family row per metric family. */
export interface ConfidenceFamilyRow {
  family: string;
  semantic_type: string;
  cells: number;
  /** null for a report written before #1517: it holds band words, which the App does not show. */
  origin_counts: ConfidenceOriginCounts | null;
  mean_confidence: string | null;
  compared: number;
  agreement_rate: string | null;
  tolerance: string | null;
  origins: string[];
}

export interface ConfidenceReportRow {
  universe_id: string;
  run_id: string;
  cutoff: string;
  generated_at: string;
  sources_connected: string[];
  families: ConfidenceFamilyRow[];
  close_matches_quality_report: boolean | null;
  /** Every family the report cross-checks against the persisted quality report: the bar
   *  fields per field in bar order (#865), any other family after them alphabetically.
   *  `matches` is null when the quality report graded nothing for the family. */
  quality_report_matches: ConfidenceQualityMatch[];
  stored_confidence_used_for_bands: boolean;
}

interface ConfidenceReportDbRow {
  universe_id: string;
  run_id: string;
  cutoff: string;
  created_at: string;
  payload: {
    generated_at?: string;
    sources_connected?: string[];
    families?: Record<
      string,
      {
        semantic_type?: string;
        cells?: number;
        by_independent_origins?: Record<string, number>;
        mean_confidence?: string;
        compared?: number;
        agreement_rate?: string | null;
        tolerance?: string | null;
        origins?: string[];
      }
    >;
    accuracy?: {
      [family: string]: AccuracyEntryDb | Record<string, unknown> | undefined;
    };
    metadata?: { stored_confidence?: { used_for_bands?: boolean } };
  };
}

/** The newest nightly confidence & accuracy report per active governed universe (#1359 / #1362). */
const CONFIDENCE_REPORT_SQL = `
  select distinct on (universe_id) universe_id, run_id, cutoff::text, created_at::text, payload
  from mart.datahub_confidence_report
  where universe_id in (select universe_id from mart.served_head)
  order by universe_id, created_at desc
`;

function originCounts(
  byOrigins: Record<string, number> | undefined,
): ConfidenceOriginCounts | null {
  if (byOrigins === undefined) return null;
  const counts: ConfidenceOriginCounts = {
    none: 0,
    one: 0,
    two: 0,
    three_or_more: 0,
  };
  for (const [origins, cells] of Object.entries(byOrigins)) {
    const n = Number(origins);
    if (n === 0) counts.none += cells;
    else if (n === 1) counts.one += cells;
    else if (n === 2) counts.two += cells;
    else counts.three_or_more += cells;
  }
  return counts;
}

function confidenceReportRows(
  rows: ConfidenceReportDbRow[],
): ConfidenceReportRow[] {
  return rows.map((row) => {
    const payload = row.payload ?? {};
    const accuracy = payload.accuracy ?? {};
    const qualityMatches: ConfidenceQualityMatch[] = [];
    for (const [family, entry] of Object.entries(accuracy)) {
      if (family === "sec_oracle" || !entry || !("matches_quality_report" in entry))
        continue;
      const accuracyEntry = entry as AccuracyEntryDb;
      qualityMatches.push({
        family,
        matches: accuracyEntry.matches_quality_report ?? null,
        mismatches: accuracyEntry.quality_report_mismatches?.length ?? 0,
      });
    }
    qualityMatches.sort(
      (a, b) =>
        qualityMatchRank(a.family) - qualityMatchRank(b.family) ||
        a.family.localeCompare(b.family),
    );
    const close = accuracy.close as AccuracyEntryDb | undefined;
    return {
      universe_id: row.universe_id,
      run_id: row.run_id,
      cutoff: row.cutoff,
      generated_at: payload.generated_at ?? row.created_at,
      sources_connected: payload.sources_connected ?? [],
      families: Object.entries(payload.families ?? {}).map(
        ([family, entry]) => ({
          family,
          semantic_type: entry.semantic_type ?? "",
          cells: entry.cells ?? 0,
          origin_counts: originCounts(entry.by_independent_origins),
          mean_confidence: entry.mean_confidence ?? null,
          compared: entry.compared ?? 0,
          agreement_rate: entry.agreement_rate ?? null,
          tolerance: entry.tolerance ?? null,
          origins: entry.origins ?? [],
        }),
      ),
      close_matches_quality_report:
        close && "matches_quality_report" in close
          ? (close.matches_quality_report ?? null)
          : null,
      quality_report_matches: qualityMatches,
      // Defaults to false on purpose: the column is a constant, and a report that does
      // not say otherwise must not be read as having used it.
      stored_confidence_used_for_bands:
        payload.metadata?.stored_confidence?.used_for_bands ?? false,
    };
  });
}

export interface VendorAccountLedgerDbRow {
  source: string;
  rows_today: number;
  failed_today: number;
  http_429_today: number;
  code_1302_today: number;
  peak_1s: number;
  peak_30s: number;
  peak_60s: number;
}

export interface VendorLimitDashboardSummary {
  id: string;
  unit: "request" | "distinct_stock";
  amount: number | null;
  window_seconds: number | null;
  share: number | null;
  divided: boolean;
  evidence_kind: EvidenceKindContract;
  checked_at: string | null;
  method: string | null;
  reason: string | null;
  source: readonly string[];
}

export interface VendorAccountEnvironmentShareSummary {
  environment: string;
  percent: number;
  admits_a_call: boolean;
  daily_share: number | null;
  rate_window_seconds: number | null;
  rate_window_share: number | null;
}

export interface VendorAccountUsageRow {
  account: string;
  holder: "api_key" | "host_ip" | "account";
  seats: readonly string[];
  outside_consumers: boolean;
  environment: string;
  percent: number;
  rows_today: number;
  failed_today: number;
  daily_share: number | null;
  daily_ratio: number | null;
  daily_ratio_text: string | null;
  daily_usage_text: string;
  peak_window_calls: number;
  rate_window_seconds: number | null;
  rate_window_share: number | null;
  window_ratio: number | null;
  window_usage_text: string;
  http_429_today: number;
  code_1302_today: number;
  evidence_kinds: readonly EvidenceKindContract[];
  not_known_limits: readonly VendorLimitDashboardSummary[];
  limits: readonly VendorLimitDashboardSummary[];
  shares_by_environment: Readonly<Record<string, VendorAccountEnvironmentShareSummary>>;
  verdict: "ok" | "warn";
  detail: string;
}

export interface Model1302DayDbRow {
  day: string;
  total_calls: number;
  http_429: number;
  code_1302: number;
}

export interface Model1302DayRow {
  environment: string;
  day: string;
  total_calls: number;
  http_429: number;
  code_1302: number;
  calls_per_minute_limit: number | null;
  max_concurrent_calls: number | null;
}

export interface DatahubStats {
  oracleOutcomes: OracleOutcomesTable;
  confidence: ConfidenceReportRow[];
  questionCoverage: QuestionCoverageRow[];
  heads: HeadStatusRow[];
  nightlyVerdicts: NightlyVerdictRow[];
  sources: SourceStatRow[];
  runs: CaptureRunRow[];
  validation: ValidationRow[];
  capacity: ValidationRow[];
  vendorAccounts: VendorAccountUsageRow[];
  model1302ByDay: Model1302DayRow[];
  traffic: TrafficSourceRow[];
  recentCalls: RecentCallRow[];
  ledgerCoverage: LedgerCoverageRow[];
}

/**
 * The CURRENT verdict of each check: the newest row per `check_name`. The order is the health endpoint's
 * (`llm_service.main.NIGHTLY_VERDICTS_SQL`) and the data engine's (`nightly_verdicts.NEWEST_SQL`): `ran_at`,
 * then `recorded_at`. The table keeps every night, so a bare `order by check_name` listed the whole history,
 * and the page showed the same check once per night (#1466). The history of one check is one click away:
 * `NIGHTLY_VERDICT_HISTORY_SQL`.
 */
export const NIGHTLY_VERDICTS_SQL = `
  select distinct on (check_name)
         check_name,
         to_char(ran_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS"Z"') as ran_at,
         ok,
         summary,
         dagster_run_id
  from mart.nightly_verdicts
  order by check_name, ran_at desc, recorded_at desc
`;

/** Every recorded verdict of one check, newest first. The bound keeps a year of nights on screen. */
export const NIGHTLY_VERDICT_HISTORY_SQL = `
  select check_name,
         to_char(ran_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS"Z"') as ran_at,
         ok,
         summary,
         dagster_run_id
  from mart.nightly_verdicts
  where check_name = $1
  order by ran_at desc, recorded_at desc
  limit 400
`;

/** The shape of a verdict name (`mart.nightly_verdicts.check_name`, `nightly_verdicts.is_valid_name`). */
const CHECK_NAME = /^[a-z0-9_]+(@[a-z0-9_.:-]+)?$/;

/** The history of one check, or null when `check` is not a verdict name (the page then shows no history). */
export async function loadVerdictHistory(
  check: string,
  principal: { principalKind: "member" | "administrator" | "service" } | null,
): Promise<NightlyVerdictRow[] | null> {
  if (principal === null || principal.principalKind !== "administrator" || !CHECK_NAME.test(check)) return null;
  return withOpsReader(async (client) => {
    const result = await client.query<NightlyVerdictRow>(NIGHTLY_VERDICT_HISTORY_SQL, [check]);
    return result.rows.map(verdictRow);
  });
}

function verdictRow(row: NightlyVerdictRow): NightlyVerdictRow {
  return {
    check_name: row.check_name,
    ran_at: row.ran_at,
    ok: row.ok === null || row.ok === undefined ? null : Boolean(row.ok),
    summary: row.summary ?? "",
    dagster_run_id: row.dagster_run_id ?? null,
  };
}

const HEADS_SQL = `
  select h.environment, h.universe_id, h.sequence, h.advanced_at::text as advanced_at,
         coalesce(h.run_id, h.head_run_id) as target_run_id,
         q.payload->>'availability' as availability,
         h.availability as head_availability,
         (select count(*)::int from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb)) c
           where c.value->>'outcome' = 'agreed') as agreed_cells,
         (select count(*)::int
            from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb))) as total_cells,
         h.freshness,
         h.age_hours::float as age_hours,
         h.limit_hours::float as limit_hours,
         h.staleness_reason,
         coalesce(q.payload->'factor_availability', '{}'::jsonb) as factor_availability,
         q.payload->'listing_ratio' as listing_ratio
  from mart.served_head h
  left join mart.datahub_quality_report q on q.run_id = coalesce(h.run_id, h.head_run_id)
  order by h.universe_id
`;

const SOURCES_SQL = `
  select source, count(*)::int as fetches_total,
         (count(*) filter (where recorded_at > now() - interval '24 hours'))::int as fetches_24h,
         max(recorded_at)::text as last_fetch
  from raw.fetches
  where ${REGISTERED_RAW_SOURCE_FILTER}
  group by source
  order by max(recorded_at) desc, count(*) desc
  limit 12
`;

// #648/#635/#578/#1523: capture-obligation completeness of the newest sentinel run,
// yesterday's reuse savings (vendor fetches avoided), the plausibility sign-check
// violation count in the newest quality report, and the corroborated share of the newest graded run.
const VALIDATION_SQL = `
  select 'capture obligations complete' as check,
         case when s.complete and s.failed_count = 0 then 'pass' else 'fail' end as verdict,
         'latest ' || to_char(s.cutoff, 'MM-DD HH24:MI') || ' — ' || (s.success_count + s.unchanged_count)
           || '/' || s.obligation_count || ' resolved' as detail
  from mart.topt_capture_status s
  where s.universe_id like any ($1::text[])
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
  select 'plausibility sign checks' as check,
         case when coalesce((q.payload->>'implausible_count')::int, 0) = 0 then 'pass' else 'violations' end as verdict,
         coalesce(q.payload->>'implausible_count', '0') || ' implausible cells in the newest report' as detail
  from mart.datahub_quality_report q
  join mart.topt_capture_status s using (run_id)
  where ${NON_CANARY_UNIVERSE_WHERE}
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
    where ${NON_CANARY_UNIVERSE_WHERE}
    order by s.cutoff desc limit 1
  ) newest
`;

// #1520 step 3 & step 5: per-seat ledger rows today (UTC), counting rows (`count(*)`),
// never `sum(cost)`, plus peak calls in 1s, 30s and 60s windows, HTTP 429 answers and
// provider code 1302 answers.
export const VENDOR_LEDGER_TODAY_SQL = `
  with today as (
    select
      source,
      ok,
      status_code,
      provider_code,
      floor(extract(epoch from called_at))::bigint as bucket_1s,
      floor(extract(epoch from called_at) / 30)::bigint as bucket_30s,
      floor(extract(epoch from called_at) / 60)::bigint as bucket_60s
    from staging.api_call_ledger
    where called_at >= ${UTC_DAY_START_SQL}
  ),
  peaks as (
    select
      s.source,
      coalesce((select max(c) from (select count(*)::int as c from today t1 where t1.source = s.source group by bucket_1s) b1), 0)::int as peak_1s,
      coalesce((select max(c) from (select count(*)::int as c from today t30 where t30.source = s.source group by bucket_30s) b30), 0)::int as peak_30s,
      coalesce((select max(c) from (select count(*)::int as c from today t60 where t60.source = s.source group by bucket_60s) b60), 0)::int as peak_60s
    from (select distinct source from today) s
  )
  select
    t.source,
    count(*)::int as rows_today,
    (count(*) filter (where not t.ok))::int as failed_today,
    (count(*) filter (where t.status_code = 429))::int as http_429_today,
    (count(*) filter (where t.provider_code = '1302'))::int as code_1302_today,
    p.peak_1s,
    p.peak_30s,
    p.peak_60s
  from today t
  join peaks p on p.source = t.source
  group by t.source, p.peak_1s, p.peak_30s, p.peak_60s
  order by t.source asc
`;

// #1520 step 5: model provider 1302 and HTTP 429 counts per UTC day in this environment's ledger.
export const MODEL_1302_BY_DAY_SQL = `
  select
    ((called_at at time zone 'utc')::date)::text as day,
    count(*)::int as total_calls,
    (count(*) filter (where status_code = 429))::int as http_429,
    (count(*) filter (where provider_code = '1302'))::int as code_1302
  from staging.api_call_ledger
  where source = $1
    and called_at >= ${UTC_DAY_START_SQL} - ($2::int - 1) * interval '1 day'
  group by 1
  order by 1 desc
`;

const CONTRACT_ENVIRONMENTS = ["preview", "staging", "production"] as const;

function normalizeDashboardEnvironment(environment?: string | null): string {
  const raw = (environment || process.env.APP_ENV || "production").trim().toLowerCase() || "production";
  return raw === "prod" ? "production" : raw;
}

/**
 * #1520 step 3: build one dashboard row per vendor account from today's ledger rows and the
 * generated vendor account shares. Verdict follows `UTILIZATION_CEILING` (0.70).
 */
export function vendorAccountRows(
  dbRows: readonly Partial<VendorAccountLedgerDbRow>[],
  environment?: string | null,
): VendorAccountUsageRow[] {
  const env = normalizeDashboardEnvironment(environment);
  const bySource = new Map<string, Partial<VendorAccountLedgerDbRow>>();
  for (const row of dbRows) {
    if (typeof row.source === "string" && row.source.length > 0 && typeof row.rows_today === "number") {
      bySource.set(row.source, row);
    }
  }

  return VENDOR_ACCOUNTS.map((account) => {
    const share = accountShare(account.name, env);
    const sharesByEnvironment: Record<string, VendorAccountEnvironmentShareSummary> = {};
    for (const envName of CONTRACT_ENVIRONMENTS) {
      const envShare = accountShare(account.name, envName);
      sharesByEnvironment[envName] = {
        environment: envName,
        percent: envShare.percent,
        admits_a_call: envShare.admitsACall,
        daily_share: envShare.dailyShare,
        rate_window_seconds: envShare.rateWindowSeconds,
        rate_window_share: envShare.rateWindowShare,
      };
    }

    let rowsToday = 0;
    let failedToday = 0;
    let http429Today = 0;
    let code1302Today = 0;
    let peak1s = 0;
    let peak30s = 0;
    let peak60s = 0;
    for (const seat of account.seats) {
      const seatRow = bySource.get(seat);
      if (!seatRow) continue;
      rowsToday += Number(seatRow.rows_today ?? 0);
      failedToday += Number(seatRow.failed_today ?? 0);
      http429Today += Number(seatRow.http_429_today ?? 0);
      code1302Today += Number(seatRow.code_1302_today ?? 0);
      peak1s += Number(seatRow.peak_1s ?? 0);
      peak30s += Number(seatRow.peak_30s ?? 0);
      peak60s += Number(seatRow.peak_60s ?? 0);
    }

    const modelLimits =
      account.name === "model_plan" && Object.prototype.hasOwnProperty.call(MODEL_SEAT.environments, env)
        ? MODEL_SEAT.environments[env]
        : null;
    const rateWindowSeconds = share.rateWindowSeconds ?? (modelLimits ? 60 : null);
    const rateWindowShare = share.rateWindowShare ?? (modelLimits ? modelLimits.calls_per_minute : null);
    const peakWindowCalls =
      rateWindowSeconds === 1
        ? peak1s
        : rateWindowSeconds === 30
          ? peak30s
          : rateWindowSeconds === 60
            ? peak60s
            : 0;

    const dailyShare = share.dailyShare;
    const dailyRatio =
      dailyShare !== null && dailyShare > 0
        ? rowsToday / dailyShare
        : dailyShare === 0
          ? rowsToday > 0
            ? Number.POSITIVE_INFINITY
            : 0
          : null;
    const dailyRatioText =
      dailyRatio === null
        ? null
        : Number.isFinite(dailyRatio)
          ? dailyRatio.toFixed(2)
          : "inf";
    const dailyUsageText =
      dailyShare !== null ? `${rowsToday} of ${dailyShare}` : `${rowsToday} rows (no daily limit)`;

    const windowRatio =
      rateWindowShare !== null && rateWindowShare > 0
        ? peakWindowCalls / rateWindowShare
        : rateWindowShare === 0
          ? peakWindowCalls > 0
            ? Number.POSITIVE_INFINITY
            : 0
          : null;
    const windowUsageText =
      rateWindowShare !== null && rateWindowSeconds !== null
        ? `${peakWindowCalls} of ${rateWindowShare} per ${rateWindowSeconds}s`
        : `${peakWindowCalls} calls (no rate figure)`;

    const limits: VendorLimitDashboardSummary[] = share.limits.map((limit) => ({
      id: limit.limitId,
      unit: limit.unit,
      amount: limit.amount,
      window_seconds: limit.windowSeconds,
      share: limit.share,
      divided: limit.divided,
      evidence_kind: limit.evidence.kind,
      checked_at: limit.evidence.checked_at,
      method: limit.evidence.method,
      reason: limit.evidence.reason,
      source: limit.evidence.source,
    }));
    const evidenceKinds = Array.from(new Set(limits.map((limit) => limit.evidence_kind)));
    const notKnownLimits = limits.filter(
      (limit) => limit.evidence_kind === "not_known" || limit.evidence_kind === "none_published",
    );

    const overDailyCeiling =
      dailyShare !== null &&
      (dailyShare === 0 ? rowsToday > 0 : rowsToday / dailyShare > UTILIZATION_CEILING);
    const overWindowShare =
      rateWindowShare !== null &&
      (rateWindowShare === 0 ? peakWindowCalls > 0 : peakWindowCalls > rateWindowShare);
    const hadRateLimitAnswer = http429Today > 0 || code1302Today > 0;
    const verdict: "ok" | "warn" =
      overDailyCeiling || overWindowShare || hadRateLimitAnswer ? "warn" : "ok";

    const dailyDetail =
      dailyShare !== null
        ? `${rowsToday} of ${dailyShare} daily share (ratio ${dailyRatioText}, ceiling ${UTILIZATION_CEILING.toFixed(2)}, ${failedToday} failed)`
        : `${rowsToday} rows today (no daily limit, ${failedToday} failed)`;
    const notKnownDetail =
      notKnownLimits.length > 0
        ? `; unmeasured=${notKnownLimits.map((l) => `${l.id}:${l.evidence_kind}`).join(",")}`
        : "";
    const detail = `${dailyDetail}; peak ${windowUsageText}; 429=${http429Today}, 1302=${code1302Today}; evidence=${evidenceKinds.join(",")}${notKnownDetail}`;

    return {
      account: account.name,
      holder: account.holder,
      seats: account.seats,
      outside_consumers: account.outside_consumers,
      environment: env,
      percent: share.percent,
      rows_today: rowsToday,
      failed_today: failedToday,
      daily_share: dailyShare,
      daily_ratio: dailyRatio,
      daily_ratio_text: dailyRatioText,
      daily_usage_text: dailyUsageText,
      peak_window_calls: peakWindowCalls,
      rate_window_seconds: rateWindowSeconds,
      rate_window_share: rateWindowShare,
      window_ratio: windowRatio,
      window_usage_text: windowUsageText,
      http_429_today: http429Today,
      code_1302_today: code1302Today,
      evidence_kinds: evidenceKinds,
      not_known_limits: notKnownLimits,
      limits,
      shares_by_environment: sharesByEnvironment,
      verdict,
      detail,
    };
  });
}

export function vendorAccountCapacityRows(rows: readonly VendorAccountUsageRow[]): ValidationRow[] {
  return rows.map((row) => ({
    check: `${row.account} (${row.seats.join(", ")}, ${row.environment}, today utc)`,
    verdict: row.verdict,
    detail: row.detail,
  }));
}

export function model1302Rows(
  dbRows: readonly Partial<Model1302DayDbRow>[],
  environment?: string | null,
): Model1302DayRow[] {
  const env = normalizeDashboardEnvironment(environment);
  const limits = Object.prototype.hasOwnProperty.call(MODEL_SEAT.environments, env)
    ? MODEL_SEAT.environments[env]
    : null;
  return dbRows
    .filter((row): row is Model1302DayDbRow => typeof row.day === "string" && row.day.length > 0)
    .map((row) => ({
      environment: env,
      day: row.day,
      total_calls: Number(row.total_calls ?? 0),
      http_429: Number(row.http_429 ?? 0),
      code_1302: Number(row.code_1302 ?? 0),
      calls_per_minute_limit: limits ? limits.calls_per_minute : null,
      max_concurrent_calls: limits ? limits.max_concurrent_calls : null,
    }));
}

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
             and e.called_at >= ${UTC_DAY_START_SQL}
           order by e.called_at desc limit 1) as last_error
  from staging.api_call_ledger l
  where l.called_at >= ${UTC_DAY_START_SQL}
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
         case when l.ok and l.payload_sha256 is not null then
           (select min(f.id) from raw.fetches f
             where f.payload_sha256 = l.payload_sha256 and f.source = l.source)
         end as landed_fetch_id
  from staging.api_call_ledger l
  order by l.called_at desc, l.id desc
  limit 40
`;

/**
 * #1521 step 6: the UTC days of the coverage table, today included. A weekly lane (the universe refresh) writes
 * rows on one day of seven, so a window of seven days could show none of them. Eight days always holds one.
 */
export const LEDGER_COVERAGE_DAYS = 8;

/**
 * #1521 step 6: key and run coverage of the call ledger, one row per UTC day, vendor and endpoint. `$1` is
 * `LEDGER_COVERAGE_DAYS`. A key counts as fetched twice when it has two successful rows with no repeat reason on
 * the day: the rule of the retired nightly verdict. The ledger is append-only, so rows written before
 * #1521 step 2 keep their null key and show on their own days. Shares and ratios are computed here, never in the App.
 */
export const LEDGER_COVERAGE_SQL = `
  with ledger as (
    select (l.called_at at time zone 'utc')::date as day, l.source, l.endpoint,
           l.ok, l.fetch_key, l.run_key, l.repeat_reason
    from staging.api_call_ledger l
    where l.called_at >= ${UTC_DAY_START_SQL} - ($1::int - 1) * interval '1 day'
  ),
  per_reason as (
    select day, source, endpoint, jsonb_object_agg(repeat_reason, rows) as by_reason
    from (
      select day, source, endpoint, repeat_reason, count(*)::int as rows
      from ledger
      where repeat_reason is not null
      group by day, source, endpoint, repeat_reason
    ) reasons
    group by day, source, endpoint
  ),
  twice as (
    select day, source, endpoint, count(*)::int as keys_twice
    from (
      select day, source, endpoint, fetch_key
      from ledger
      where ok and fetch_key is not null and repeat_reason is null
      group by day, source, endpoint, fetch_key
      having count(*) > 1
    ) repeated
    group by day, source, endpoint
  ),
  totals as (
    select day, source, endpoint,
           count(*)::int as calls,
           (count(*) filter (where fetch_key is null))::int as no_key,
           round(count(*) filter (where run_key is not null)::numeric / count(*), 4)::text as run_share,
           round(count(*) filter (where run_key is not null)::numeric / nullif(count(distinct run_key), 0), 1)::text
             as calls_per_run
    from ledger
    group by day, source, endpoint
  )
  select to_char(t.day, 'YYYY-MM-DD') as day, t.source, t.endpoint, t.calls, t.no_key,
         coalesce(r.by_reason, '{}'::jsonb) as by_reason,
         coalesce(w.keys_twice, 0) as keys_twice,
         t.run_share, t.calls_per_run
  from totals t
  left join per_reason r using (day, source, endpoint)
  left join twice w using (day, source, endpoint)
  order by t.day desc, t.source, t.endpoint
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

interface HeadDbRow extends Omit<HeadStatusRow, "factors" | "listing_ratio"> {
  environment?: string | null;
  head_availability?: string | null;
  factor_availability: Record<string, FactorGrade>;
  listing_ratio?: unknown;
}

const RATIO_OUTCOMES: ListingRatioOutcome[] = ["unparsed", "not_measured", "parsed", "ordinary"];

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** The ratio block of a quality report, ordered for reading. null when the report has none. */
export function listingRatioReport(block: unknown): ListingRatioReport | null {
  if (block === null || typeof block !== "object") return null;
  const { counts, cells } = block as { counts?: Record<string, unknown>; cells?: Record<string, unknown> };
  if (counts === undefined || counts === null || cells === undefined || cells === null) return null;
  const rows: ListingRatioCell[] = [];
  for (const [key, raw] of Object.entries(cells)) {
    const entry = raw as Record<string, unknown>;
    const outcome = RATIO_OUTCOMES.find((candidate) => candidate === entry.outcome);
    if (outcome === undefined) continue;
    rows.push({
      listing_id: text(entry.listing_id) ?? key,
      issuer_id: text(entry.issuer_id),
      outcome,
      listing_units_per_share: text(entry.listing_units_per_share),
      pattern: text(entry.pattern),
      title: text(entry.title),
      reason: text(entry.reason),
      accession: text(entry.accession),
      form: text(entry.form),
      filed: text(entry.filed),
    });
  }
  rows.sort(
    (a, b) =>
      RATIO_OUTCOMES.indexOf(a.outcome) - RATIO_OUTCOMES.indexOf(b.outcome) || a.listing_id.localeCompare(b.listing_id),
  );
  return {
    counts: {
      parsed: Number(counts.parsed ?? 0),
      ordinary: Number(counts.ordinary ?? 0),
      unparsed: Number(counts.unparsed ?? 0),
      not_measured: Number(counts.not_measured ?? 0),
    },
    cells: rows,
  };
}

export async function loadDatahubStats(): Promise<DatahubStats> {
  return withOpsReader(async (client) => {
    // $1 of VALIDATION_SQL, PLAUSIBILITY_SQL and CORROBORATION_SQL: the id patterns of the sentinel universes (#1522).
    const sentinelLikes = idPrefixLikesOfRole("sentinel");
    const [
      heads,
      sources,
      runs,
      canary,
      reuse,
      plausibility,
      corroboration,
      vendorLedgerToday,
      model1302Daily,
      reuseCap,
      dbSize,
      duration,
      traffic,
      recentCalls,
      ledgerCoverage,
      questionCoverage,
      confidence,
      oracleOutcomes,
      nightlyVerdicts,
    ] = await Promise.all([
      client.query<HeadDbRow>(HEADS_SQL),
      client.query<SourceStatRow>(SOURCES_SQL),
      client.query<CaptureRunRow>(RUNS_SQL),
      client.query<ValidationRow>(VALIDATION_SQL, [sentinelLikes]),
      client.query<ValidationRow>(REUSE_SQL),
      client.query<ValidationRow>(PLAUSIBILITY_SQL, [sentinelLikes]),
      client.query<ValidationRow>(CORROBORATION_SQL, [sentinelLikes]),
      client.query<VendorAccountLedgerDbRow>(VENDOR_LEDGER_TODAY_SQL),
      client.query<Model1302DayDbRow>(MODEL_1302_BY_DAY_SQL, [MODEL_SEAT.seat, LEDGER_COVERAGE_DAYS]),
      client.query<ValidationRow>(CAPACITY_REUSE_SQL),
      client.query<ValidationRow>(CAPACITY_DB_SQL),
      client.query<ValidationRow>(CAPACITY_DURATION_SQL),
      client.query<TrafficSourceRow>(TRAFFIC_SQL),
      client.query<RecentCallRow>(RECENT_CALLS_SQL),
      client.query<LedgerCoverageRow>(LEDGER_COVERAGE_SQL, [LEDGER_COVERAGE_DAYS]),
      client.query<QuestionCoverageDbRow>(QUESTION_COVERAGE_SQL),
      client.query<ConfidenceReportDbRow>(CONFIDENCE_REPORT_SQL),
      client.query<OracleOutcomeDbRow>(ORACLE_OUTCOMES_SQL),
      client.query<NightlyVerdictRow>(NIGHTLY_VERDICTS_SQL),
    ]);
    const activeEnv =
      process.env.APP_ENV ||
      heads.rows.find((row) => typeof row.environment === "string" && row.environment.length > 0)?.environment ||
      "production";
    const vendorAccounts = vendorAccountRows(vendorLedgerToday.rows, activeEnv);
    const model1302ByDay = model1302Rows(model1302Daily.rows, activeEnv);
    return {
      oracleOutcomes: oracleOutcomesTable(
        oracleOutcomes.rows,
        heads.rows.map((row) => row.universe_id),
        confidence.rows.map((row) => row.universe_id),
      ),
      confidence: confidenceReportRows(confidence.rows),
      questionCoverage: questionCoverageRows(questionCoverage.rows),
      nightlyVerdicts: nightlyVerdicts.rows.map(verdictRow),
      heads: heads.rows.map((row: HeadDbRow) => {
        const parsedAvail =
          row.availability !== null && row.availability !== undefined
            ? parseFloat(row.availability)
            : Number.NaN;
        const availNum = Number.isNaN(parsedAvail) ? null : parsedAvail;
        const headWithheld =
          row.head_availability !== undefined
            ? row.head_availability !== "available"
            : availNum === null && row.availability !== "available";
        const isAvailDegraded =
          (availNum !== null &&
            availNum < ACCEPTED_SERVICE_OBJECTIVES.minimum_availability) ||
          headWithheld;
        const corroborationRatio =
          row.total_cells !== null && row.total_cells > 0 && row.agreed_cells !== null
            ? row.agreed_cells / row.total_cells
            : null;
        const isCorroborationDegraded =
          !isCanaryUniverse(row.universe_id) &&
          corroborationRatio !== null &&
          corroborationRatio < ACCEPTED_SERVICE_OBJECTIVES.minimum_corroborated_share;
        const freshness: HeadFreshness = resolveHeadFreshness(
          row.freshness,
          row.universe_id,
          row.environment,
        );
        const isFreshnessDegraded =
          freshness !== "fresh" && freshness !== "manual";

        const degraded_reasons: string[] = [];
        if (isAvailDegraded) {
          if (
            availNum !== null &&
            availNum < ACCEPTED_SERVICE_OBJECTIVES.minimum_availability
          ) {
            degraded_reasons.push(
              `availability (${(availNum * 100).toFixed(1)}% < ${(ACCEPTED_SERVICE_OBJECTIVES.minimum_availability * 100).toFixed(0)}%)`,
            );
          } else {
            const withheldLabel =
              row.head_availability && row.head_availability !== "available"
                ? row.head_availability
                : row.availability && row.availability !== "available"
                  ? row.availability
                  : "unavailable";
            degraded_reasons.push(`availability (${withheldLabel})`);
          }
        }
        if (isCorroborationDegraded && corroborationRatio !== null) {
          degraded_reasons.push(
            `corroboration (${(corroborationRatio * 100).toFixed(1)}% < ${(ACCEPTED_SERVICE_OBJECTIVES.minimum_corroborated_share * 100).toFixed(0)}%)`,
          );
        }
        if (isFreshnessDegraded) {
          const detail = row.staleness_reason
            ? `${freshness}: ${row.staleness_reason}`
            : freshness;
          degraded_reasons.push(`freshness (${detail})`);
        }
        const is_degraded = degraded_reasons.length > 0;

        return {
          universe_id: row.universe_id,
          sequence: row.sequence,
          advanced_at: row.advanced_at,
          target_run_id: row.target_run_id,
          availability: row.availability,
          agreed_cells: row.agreed_cells,
          total_cells: row.total_cells,
          freshness,
          age_hours:
            row.age_hours !== null && row.age_hours !== undefined
              ? Number(row.age_hours)
              : null,
          limit_hours:
            row.limit_hours !== null && row.limit_hours !== undefined
              ? Number(row.limit_hours)
              : null,
          staleness_reason: row.staleness_reason ?? null,
          is_degraded,
          is_avail_degraded: isAvailDegraded,
          is_corroboration_degraded: isCorroborationDegraded,
          degraded_reasons,
          factors: Object.entries(row.factor_availability ?? {}).map(
            ([factorId, grade]) => ({
              factor_id: factorId,
              required_semantics: grade.required_semantics,
              complete_subjects: grade.complete_subjects,
              universe_subjects: grade.universe_subjects,
              ratio: grade.ratio,
            }),
          ),
          listing_ratio: listingRatioReport(row.listing_ratio),
        };
      }),
      sources: sources.rows,
      runs: runs.rows,
      validation: [
        ...canary.rows,
        ...reuse.rows,
        ...plausibility.rows,
        ...corroboration.rows,
      ],
      capacity: [
        ...vendorAccountCapacityRows(vendorAccounts),
        ...reuseCap.rows,
        ...dbSize.rows,
        ...duration.rows,
      ],
      vendorAccounts,
      model1302ByDay,
      traffic: traffic.rows,
      recentCalls: recentCalls.rows,
      ledgerCoverage: ledgerCoverage.rows,
    };
  });
}
