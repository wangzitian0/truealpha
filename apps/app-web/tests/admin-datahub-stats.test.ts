/**
 * #641 D5: the datahub stats loader — heads carry both availabilities (the
 * capture-level headline and the #644 per-factor grades), sources and runs
 * pass through. Fake client via the ops test override.
 *
 * Run standalone: `bun run tests/admin-datahub-stats.test.ts`.
 */

import { idPrefixLikesOfRole } from "../src/contracts/universes";
import { listingRatioReport, loadDatahubStats } from "../src/server/admin/datahub-stats";
import { __setTestOpsClient } from "../src/server/admin/ops";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const headRows = [
  {
    universe_id: "universe:qqq-us-2026-06-30",
    sequence: 0,
    advanced_at: "2026-08-18 23:20:00+00",
    target_run_id: "capture-run:" + "9".repeat(64),
    availability: "0.7819",
    agreed_cells: 102,
    total_cells: 102,
    freshness: "fresh",
    age_hours: 12.5,
    limit_hours: 72.0,
    staleness_reason: null,
    factor_availability: {
      gross_profit_per_employee: {
        required_semantics: ["financial-fact"],
        complete_subjects: 13,
        universe_subjects: 102,
        ratio: "0.1275",
      },
    },
    // #1519 step 1a: the block that the data engine writes into the quality report. Keys come back from jsonb in
    // their own order, and the loader orders the rows for reading.
    listing_ratio: {
      counts: { parsed: 1, ordinary: 1, unparsed: 1, not_measured: 0 },
      cells: {
        "listing:xnas:aapl": {
          issuer_id: "issuer:cik:0000320193",
          listing_id: "listing:xnas:aapl",
          outcome: "ordinary",
          listing_units_per_share: "1",
          pattern: "plain_share_title",
          title: "Common Stock, $0.00001 par value per share",
          reason: null,
          accession: "0000320193-25-000079",
          form: "10-K",
          filed: "2025-10-31",
        },
        "listing:xnas:pdd": {
          issuer_id: "issuer:cik:0001737806",
          listing_id: "listing:xnas:pdd",
          outcome: "parsed",
          listing_units_per_share: "4",
          pattern: "depositary_each_representing",
          title: "American Depositary Shares, each representing four Class A ordinary shares",
          reason: null,
          accession: "0001737806-26-000005",
          form: "20-F",
          filed: "2026-04-25",
        },
        "listing:xnas:zzzz": {
          issuer_id: "issuer:cik:0000000001",
          listing_id: "listing:xnas:zzzz",
          outcome: "unparsed",
          listing_units_per_share: null,
          pattern: "unparsed",
          title: "Units, each consisting of one share and one warrant",
          reason: "unfamiliar_title",
          accession: "0000000001-26-000001",
          form: "10-K",
          filed: "2026-02-20",
        },
      },
    },
  },
];
const sourceRows = [
  {
    source: "yahoo",
    fetches_total: 728,
    fetches_24h: 123,
    last_fetch: "2026-08-18",
  },
];
// #729: the external call ledger — one vendor today, and one request each way.
const trafficRows = [
  {
    source: "twelvedata",
    calls: 42,
    failed: 21,
    landed: 21,
    avg_ms: 310,
    last_call: "2026-09-05 22:19:44+00",
    last_error: "No data is available on the specified dates",
  },
];
const recentCallRows = [
  {
    id: 2,
    called_at: "2026-09-05 22:19:44+00",
    source: "twelvedata",
    endpoint: "time_series",
    caller: "twelve_data_origin",
    ok: true,
    status_code: 200,
    duration_ms: 290,
    error: null,
    run_key: "dagster:abc",
    landed_fetch_id: 6540,
  },
  {
    id: 1,
    called_at: "2026-09-05 22:19:33+00",
    source: "twelvedata",
    endpoint: "eod",
    caller: "twelve_data_origin",
    ok: false,
    status_code: 400,
    duration_ms: 330,
    error: "No data is available on the specified dates",
    run_key: "dagster:abc",
    landed_fetch_id: null,
  },
];
const runRows = [
  {
    run_id: "capture-run:" + "9".repeat(64),
    universe_id: "universe:qqq-us-2026-06-30",
    cutoff: "2026-08-18 23:20:00+00",
    obligations: 408,
    resolved: 408,
    failed: 0,
    complete: true,
  },
];

const coverageRows = [
  {
    universe_id: "universe:topt-us-2026-03-31",
    cutoff: "2026-03-31 00:00:00+00",
    created_at: "2026-09-13 09:07:00+00",
    payload: {
      generated_at: "2026-09-13T09:07:00+00:00",
      requirements_sha256: "a".repeat(64),
      questions: {
        q1: {
          text: "leverage?",
          column: "mart.topt_gppe_results.gppe",
          tracking_issue: "#528",
          answered: 18,
          unavailable: { missing_gross_profit: 2 },
          missing: 0,
          denominator: 20,
        },
        q3: {
          text: "supply chain?",
          column: null,
          tracking_issue: "#772",
          answered: 0,
          unavailable: {},
          missing: 20,
          denominator: 20,
        },
      },
    },
  },
  {
    // #1206: PEG has no column on QQQ, so every subject is missing and in the denominator.
    universe_id: "universe:qqq-us-2026-06-30",
    cutoff: "2026-06-30 00:00:00+00",
    created_at: "2026-10-08 09:07:00+00",
    payload: {
      generated_at: "2026-10-08T09:07:00+00:00",
      requirements_sha256: "a".repeat(64),
      questions: {
        q2: {
          text: "valuation vs growth?",
          column: null,
          tracking_issue: "#284",
          answered: 0,
          unavailable: {},
          missing: 100,
          denominator: 100,
        },
      },
    },
  },
];

const confidenceRows = [
  {
    universe_id: "universe:qqq-us-2026-06-30",
    run_id: "capture-run:" + "9".repeat(64),
    cutoff: "2026-09-14 23:20:00+00",
    created_at: "2026-09-16 00:45:00+00",
    payload: {
      generated_at: "2026-09-16T00:45:00+00:00",
      sources_connected: ["nasdaq-index", "sec-company-facts", "sec-nport", "twelve-data", "yahoo"],
      families: {
        close: {
          semantic_type: "market-price",
          cells: 102,
          by_independent_origins: { "1": 1, "2": 98, "3": 3 },
          mean_confidence: "0.8512",
          compared: 101,
          agreement_rate: "1.0000",
          tolerance: "reconciliation-policy:" + "b".repeat(64),
          origins: ["origin:twelve-data:v1", "origin:yahoo:v1"],
        },
        // A report written before #1517 holds band words: the App shows a dash for it.
        revenue: {
          semantic_type: "financial-fact",
          cells: 102,
          high: 0,
          medium: 0,
          low: 97,
          missing: 5,
          compared: 0,
          agreement_rate: null,
          tolerance: null,
          origins: ["origin:sec-company-facts:v1"],
        },
      },
      // Deliberately out of bar order: jsonb hands keys back in its own order, and the
      // loader must impose the bar's.
      accuracy: {
        volume: {
          matches_quality_report: false,
          quality_report_mismatches: ["listing:xnas:aapl"],
        },
        close: { matches_quality_report: true, quality_report_mismatches: [] },
        open: { matches_quality_report: true, quality_report_mismatches: [] },
        sec_oracle: {
          issuers_compared: 5,
          per_field: {
            revenue: { compared: 5, agreed: 4, agreement_rate: "0.8000" },
          },
        },
      },
      metadata: { stored_confidence: { used_for_bands: false } },
    },
  },
];

const nightlyVerdictRows = [
  {
    check_name: "output_invariants",
    ran_at: "2026-10-20 00:15:00Z",
    ok: true,
    summary: "ok (26/26 passed)",
    dagster_run_id: "dagster-run-123",
  },
  // A pending verdict: the check ran and could not judge yet (#1466). It is neither ok nor a failure.
  {
    check_name: "release_fetch_proof",
    ran_at: "2026-10-20 06:15:00Z",
    ok: null,
    summary: "pending",
    dagster_run_id: null,
  },
];

{
  __setTestOpsClient({
    query: async (sql: string) => {
      if (typeof sql === "string" && sql.includes("nightly_verdicts"))
        return { rows: nightlyVerdictRows } as never;
      if (typeof sql === "string" && sql.includes("datahub_confidence_report"))
        return { rows: confidenceRows } as never;
      if (typeof sql === "string" && sql.includes("question_coverage_report"))
        return { rows: coverageRows } as never;
      if (typeof sql === "string" && sql.includes("served_head"))
        return { rows: headRows } as never;
      // "as check" FIRST: capacity SQLs also mention raw.fetches, and matching the
      // sources stub first fed them SourceStatRow shapes while the count-only
      // assertion stayed green (review on #678).
      if (typeof sql === "string" && sql.includes("as check")) {
        const checkMatch = sql.match(/select\s+'([^']+)'\s+as\s+check/i);
        return {
          rows: [
            { check: checkMatch?.[1] ?? "unknown", verdict: "pass", detail: "stubbed" },
          ],
        } as never;
      }
      // #729: the ledger reads also join raw.fetches, so they are matched BEFORE the
      // sources stub, on the ledger table itself; per-source aggregate vs recent list.
      if (typeof sql === "string" && sql.includes("api_call_ledger")) {
        return {
          rows: sql.includes("group by l.source")
            ? trafficRows
            : recentCallRows,
        } as never;
      }
      if (typeof sql === "string" && sql.includes("raw.fetches"))
        return { rows: sourceRows } as never;
      return { rows: runRows } as never;
    },
  } as never);
  const stats = await loadDatahubStats();
  assert(
    stats.traffic.length === 1 && stats.traffic[0].source === "twelvedata",
    "traffic rows pass through",
  );
  assert(
    stats.nightlyVerdicts.length === 2 &&
      stats.nightlyVerdicts[0].check_name === "output_invariants" &&
      stats.nightlyVerdicts[0].ok === true &&
      stats.nightlyVerdicts[0].summary === "ok (26/26 passed)" &&
      stats.nightlyVerdicts[1].ok === null,
    "nightly verdicts from mart.nightly_verdicts are surfaced on /admin/datahub (#1346)",
  );
  assert(
    stats.questionCoverage.length === 2,
    "the newest coverage report per universe is surfaced",
  );
  assert(
    stats.confidence.length === 1 && stats.confidence[0].families.length === 2,
    "the newest confidence report per universe is surfaced with its families",
  );
  const close = stats.confidence[0].families.find((f) => f.family === "close");
  assert(
    close !== undefined &&
      close.origin_counts?.none === 0 &&
      close.origin_counts?.one === 1 &&
      close.origin_counts?.two === 98 &&
      close.origin_counts?.three_or_more === 3 &&
      close.mean_confidence === "0.8512" &&
      close.agreement_rate === "1.0000" &&
      close.origins.length === 2,
    "close is counted by independent origins with its mean confidence and agreement rate",
  );
  const legacyRevenue = stats.confidence[0].families.find((f) => f.family === "revenue");
  assert(
    legacyRevenue !== undefined &&
      legacyRevenue.origin_counts === null &&
      legacyRevenue.mean_confidence === null,
    "a report written before #1517 shows no origin counts instead of zeros",
  );
  assert(
    stats.confidence[0].oracle_fields[0].field === "revenue" &&
      stats.confidence[0].oracle_fields[0].agreed === 4 &&
      stats.confidence[0].close_matches_quality_report === true &&
      stats.confidence[0].stored_confidence_used_for_bands === false,
    "the accuracy oracle and the stored-confidence disclaimer pass through",
  );
  assert(
    JSON.stringify(stats.confidence[0].quality_report_matches) ===
      JSON.stringify([
        { family: "open", matches: true, mismatches: 0 },
        { family: "close", matches: true, mismatches: 0 },
        { family: "volume", matches: false, mismatches: 1 },
      ]),
    "every family the report cross-checks against the quality report is surfaced per field in bar order (#865), the oracle never among them",
  );
  const q1 = stats.questionCoverage[0].questions.find(
    (q) => q.question === "q1",
  );
  assert(
    q1 !== undefined &&
      q1.answered === 18 &&
      q1.unavailable_total === 2 &&
      q1.scope === "issuer" &&
      q1.top_reasons === "missing_gross_profit=2",
    "q1 flattened with its top reasons and scope",
  );
  const q3 = stats.questionCoverage[0].questions.find(
    (q) => q.question === "q3",
  );
  assert(
    q3 !== undefined &&
      q3.missing === 20 &&
      q3.column === null &&
      q3.tracking_issue === "#772",
    "a question without a column is missing and names its owner",
  );
  const qqqReport = stats.questionCoverage.find(
    (r) => r.universe_id === "universe:qqq-us-2026-06-30",
  );
  const qqqQ2 = qqqReport?.questions.find((q) => q.question === "q2");
  assert(
    qqqQ2 !== undefined &&
      qqqQ2.answered === 0 &&
      qqqQ2.missing === 100 &&
      qqqQ2.denominator === 100,
    "q2 on QQQ is missing for every subject and stays in the denominator",
  );
  assert(
    stats.traffic[0].failed === 21,
    "failed requests are counted, not hidden behind landed rows",
  );
  assert(stats.recentCalls.length === 2, "recent calls pass through");
  assert(
    stats.recentCalls[0].landed_fetch_id === 6540,
    "a successful call names the raw.fetches row it became",
  );
  assert(
    stats.recentCalls[1].ok === false && stats.recentCalls[1].error !== null,
    "a failed call carries the vendor's error",
  );
  assert(stats.heads.length === 1, "one governed head");
  assert(
    stats.heads[0].availability === "0.7819",
    "capture-level headline rides along",
  );
  assert(
    stats.heads[0].freshness === "fresh" &&
      stats.heads[0].age_hours === 12.5 &&
      stats.heads[0].limit_hours === 72.0 &&
      stats.heads[0].staleness_reason === null,
    "head freshness columns pass through from mart.served_head",
  );
  assert(
    stats.heads[0].is_degraded === true &&
      stats.heads[0].is_avail_degraded === true &&
      stats.heads[0].is_corroboration_degraded === false &&
      stats.heads[0].degraded_reasons.length === 1 &&
      stats.heads[0].degraded_reasons[0].startsWith("availability (78.2% < 95"),
    "head degraded status is server-computed from objectives",
  );
  assert(
    stats.heads[0].factors.length === 1,
    "factor grades unpacked from the payload map",
  );
  assert(
    stats.heads[0].factors[0].factor_id === "gross_profit_per_employee",
    "factor id from the map key",
  );
  assert(
    stats.heads[0].factors[0].ratio === "0.1275",
    "the honest number the 0.78 headline hid",
  );
  const ratio = stats.heads[0].listing_ratio;
  assert(
    ratio !== null &&
      ratio.counts.parsed === 1 &&
      ratio.counts.ordinary === 1 &&
      ratio.counts.unparsed === 1 &&
      ratio.counts.not_measured === 0,
    "the engine's counts of the depositary ratio pass through, the App computes none",
  );
  assert(
    ratio !== null &&
      ratio.cells.map((cell) => cell.listing_id).join(",") ===
        "listing:xnas:zzzz,listing:xnas:pdd,listing:xnas:aapl",
    "the unparsed listing comes first, then the parsed ratio, then the ordinary shares",
  );
  assert(
    ratio !== null &&
      ratio.cells[0].title === "Units, each consisting of one share and one warrant" &&
      ratio.cells[0].reason === "unfamiliar_title" &&
      ratio.cells[0].listing_units_per_share === null &&
      ratio.cells[1].listing_units_per_share === "4" &&
      ratio.cells[1].accession === "0001737806-26-000005",
    "an unparsed listing keeps its title text and reason; a parsed one keeps its ratio and filing",
  );
  assert(
    listingRatioReport(null) === null && listingRatioReport({}) === null && listingRatioReport("x") === null,
    "a quality report written before the ratio block shows no block, and never a block of zeros",
  );
  assert(stats.sources[0].fetches_total === 728, "source stats pass through");
  assert(stats.runs[0].resolved === 408, "run stats pass through");
  assert(stats.validation.length === 4, "four validation checks, one row each");
  assert(stats.capacity.length === 4, "four capacity limits, one row each");
  assert(
    stats.capacity.every(
      (row) =>
        typeof row.verdict === "string" && typeof row.detail === "string",
    ),
    "capacity rows carry the validation shape, not a mis-stubbed one",
  );
  assert(
    stats.validation[0].verdict === "pass",
    "validation verdicts pass through",
  );
  __setTestOpsClient(null);
}

// #434 / #1237 / #1272: stale or withheld heads must set is_degraded=true and
// PLAUSIBILITY_SQL / CORROBORATION_SQL must exclude canary% runs.
{
  const capturedSqls: string[] = [];
  const capturedParams = new Map<string, readonly unknown[] | undefined>();
  const staleAndWithheldHeads = [
    {
      universe_id: "universe:qqq-us-2026-06-30",
      sequence: 1,
      advanced_at: "2026-09-21 17:48:00+00",
      target_run_id: "capture-run:" + "a".repeat(64),
      availability: "1.0000",
      head_availability: "available",
      agreed_cells: 102,
      total_cells: 102,
      freshness: "stale",
      age_hours: 537.5,
      limit_hours: 192.0,
      staleness_reason: "older_than_8d",
      factor_availability: {},
    },
    {
      universe_id: "universe:sp500-us-2026-06-30",
      sequence: 2,
      advanced_at: "2026-09-01 00:00:00+00",
      target_run_id: "capture-run:" + "b".repeat(64),
      availability: "1.0000",
      head_availability: "unavailable",
      agreed_cells: 500,
      total_cells: 500,
      freshness: "stale",
      age_hours: 750.0,
      limit_hours: 192.0,
      staleness_reason: "older_than_30d",
      factor_availability: {},
    },
    {
      universe_id: "universe:topt-us-2026-03-31",
      sequence: 3,
      advanced_at: "2026-09-01 00:00:00+00",
      target_run_id: "capture-run:" + "c".repeat(64),
      availability: "unavailable",
      agreed_cells: 20,
      total_cells: 20,
      freshness: "fresh",
      age_hours: 10.0,
      limit_hours: 72.0,
      staleness_reason: null,
      factor_availability: {},
    },
    {
      environment: "production",
      universe_id: "universe:canary-us-2026-10-19",
      sequence: 4,
      advanced_at: "2026-10-19 06:00:00+00",
      target_run_id: "capture-run:" + "d".repeat(64),
      availability: "1.0000",
      head_availability: "available",
      agreed_cells: 0,
      total_cells: 3,
      freshness: "fresh",
      age_hours: 1.2,
      limit_hours: 72.0,
      staleness_reason: null,
      factor_availability: {},
    },
    {
      environment: "staging",
      universe_id: "universe:qqq-us-t5",
      sequence: 5,
      advanced_at: "2026-10-12 06:00:00+00",
      target_run_id: "capture-run:" + "e".repeat(64),
      availability: "1.0000",
      head_availability: "available",
      agreed_cells: 100,
      total_cells: 100,
      freshness: "stale",
      age_hours: 170.0,
      limit_hours: 72.0,
      staleness_reason: "older_than_3d",
      factor_availability: {},
    },
  ];

  __setTestOpsClient({
    query: async (sql: string, params?: readonly unknown[]) => {
      capturedSqls.push(sql);
      capturedParams.set(sql, params);
      if (typeof sql === "string" && sql.includes("nightly_verdicts"))
        return { rows: [] } as never;
      if (typeof sql === "string" && sql.includes("datahub_confidence_report"))
        return { rows: [] } as never;
      if (typeof sql === "string" && sql.includes("question_coverage_report"))
        return { rows: [] } as never;
      if (typeof sql === "string" && sql.includes("served_head"))
        return { rows: staleAndWithheldHeads } as never;
      if (typeof sql === "string" && sql.includes("as check"))
        return {
          rows: [{ check: "stub", verdict: "pass", detail: "stubbed" }],
        } as never;
      if (typeof sql === "string" && sql.includes("api_call_ledger"))
        return { rows: [] } as never;
      if (typeof sql === "string" && sql.includes("raw.fetches"))
        return { rows: [] } as never;
      return { rows: [] } as never;
    },
  } as never);

  const stats = await loadDatahubStats();
  assert(
    stats.heads[0].is_degraded === true &&
      stats.heads[0].degraded_reasons.some((r) => r.includes("freshness")),
    "stale head with 1.0000 capture availability and 100% corroboration is still degraded",
  );
  assert(
    stats.heads[1].is_degraded === true &&
      stats.heads[1].is_avail_degraded === true &&
      stats.heads[1].degraded_reasons.some((r) => r.includes("availability")),
    "withheld head (head_availability !== 'available') is degraded on availability",
  );
  assert(
    stats.heads[2].is_degraded === true &&
      stats.heads[2].is_avail_degraded === true,
    "head with non-numeric 'unavailable' availability is degraded",
  );
  assert(
    stats.heads[3].universe_id === "universe:canary-us-2026-10-19" &&
      stats.heads[3].is_corroboration_degraded === false &&
      stats.heads[3].is_degraded === false,
    "canary% head is exempt from multi-vendor corroboration degradation (#1362)",
  );
  assert(
    stats.heads[4].universe_id === "universe:qqq-us-t5" &&
      stats.heads[4].freshness === "manual" &&
      stats.heads[4].is_degraded === false,
    "unscheduled staging universe:qqq-% head is graded as manual and not false-degraded (#1359, #1362)",
  );

  const plausibilitySql = capturedSqls.find((s) =>
    s.includes("'plausibility oracle' as check"),
  );
  const corroborationSql = capturedSqls.find((s) =>
    s.includes("'price corroboration' as check"),
  );
  const coverageSql = capturedSqls.find((s) =>
    s.includes("from mart.question_coverage_report"),
  );
  const confidenceSql = capturedSqls.find((s) =>
    s.includes("from mart.datahub_confidence_report"),
  );
  const sourcesSql = capturedSqls.find(
    (s) => s.includes("from raw.fetches") && s.includes("fetches_total"),
  );
  // The sentinel patterns come from the universe record, bound as $1. An empty array would exclude nothing.
  const sentinelLikes = idPrefixLikesOfRole("sentinel");
  assert(sentinelLikes.length > 0, "the record holds at least one sentinel universe");
  assert(
    plausibilitySql !== undefined &&
      plausibilitySql.toLowerCase().includes("not like all ($1::text[])") &&
      JSON.stringify(capturedParams.get(plausibilitySql)?.[0]) === JSON.stringify(sentinelLikes),
    "PLAUSIBILITY_SQL filters out the sentinel universes of the record",
  );
  assert(
    corroborationSql !== undefined &&
      corroborationSql.toLowerCase().includes("not like all ($1::text[])") &&
      JSON.stringify(capturedParams.get(corroborationSql)?.[0]) === JSON.stringify(sentinelLikes),
    "CORROBORATION_SQL filters out the sentinel universes of the record",
  );
  assert(
    coverageSql !== undefined &&
      coverageSql.includes("select universe_id from mart.served_head"),
    "QUESTION_COVERAGE_SQL scopes to active governed heads in mart.served_head (#1359, #1362)",
  );
  assert(
    confidenceSql !== undefined &&
      confidenceSql.includes("select universe_id from mart.served_head"),
    "CONFIDENCE_REPORT_SQL scopes to active governed heads in mart.served_head (#1359, #1362)",
  );
  assert(
    sourcesSql !== undefined &&
      sourcesSql.includes("-[0-9]{8}T[0-9]{4}$"),
    "SOURCES_SQL filters out legacy run-suffixed source strings (#1362)",
  );
  const trafficSql = capturedSqls.find(
    (s) => s.includes("from staging.api_call_ledger l") && s.includes("group by l.source"),
  );
  const capacityTdSql = capturedSqls.find((s) =>
    s.includes("'twelvedata budget (this env, today utc)' as check"),
  );
  const recentCallsSql = capturedSqls.find(
    (s) => s.includes("from staging.api_call_ledger l") && s.includes("limit 40"),
  );
  assert(
    trafficSql !== undefined &&
      trafficSql.includes("at time zone 'utc'") &&
      trafficSql.includes("f.payload_sha256 = l.payload_sha256 and f.source = l.source"),
    "TRAFFIC_SQL uses explicit UTC day boundary and joins raw.fetches by payload_sha256 and source (#1363)",
  );
  assert(
    capacityTdSql !== undefined && capacityTdSql.includes("at time zone 'utc'"),
    "CAPACITY_TD_SQL uses explicit UTC day boundary (#1363)",
  );
  assert(
    recentCallsSql !== undefined &&
      recentCallsSql.includes("at time zone 'utc'") &&
      recentCallsSql.includes("f.payload_sha256 = l.payload_sha256 and f.source = l.source"),
    "RECENT_CALLS_SQL formats called_at in UTC and joins raw.fetches by payload_sha256 and source (#1363)",
  );

  __setTestOpsClient(null);
}

console.log("#641 admin datahub stats loader passed");


