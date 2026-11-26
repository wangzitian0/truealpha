/**
 * Wave 2 (#1747): Verify full admin fact dashboards, single backend freshness truth,
 * 3-dim availability, peg_reason_codes, unvisited themes, entity themes, and backtest errors.
 */

import { renderToStaticMarkup } from "react-dom/server";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => ({
    principalKind: "administrator",
    context: {
      contextId: "ctx:truth-test",
      principalId: "principal:admin",
      tenantId: "tenant:truealpha",
      sessionId: "sess:truth-test",
      authenticationMethod: "session",
      issuedAt: "2026-11-25T00:00:00Z",
      expiresAt: "2026-11-25T12:00:00Z",
    },
    requestOpts: { headers: { Cookie: "ta_session=test" } },
  }),
}));

const { __setApiFetch } = await import("../src/client/api");
const { default: AdminOpsPage } = await import("../src/app/admin/page");
const { default: AdminDatahubPage } = await import("../src/app/admin/datahub/page");
const { default: CoveragePage } = await import("../src/app/research/coverage/page");
const { default: RankingsPage } = await import("../src/app/research/rankings/page");
const { default: ThemesPage } = await import("../src/app/research/themes/page");
const { default: EntityDetailPage } = await import("../src/app/research/entities/[id]/page");
const { default: BacktestDetailPage } = await import("../src/app/research/backtest/[id]/page");

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertCleanHtml(html: string, label: string): void {
  assert(!/\bundefined\b/.test(html), `${label} leaked undefined`);
  assert(!html.includes("[object Object]"), `${label} leaked [object Object]`);
  assert(!/\bNaN\b/.test(html), `${label} leaked NaN`);
  assert(!/SEC oracle:/i.test(html), `${label} leaked retired SEC oracle text`);
}

let mode: "rich" | "empty" = "rich";
const RUN_ID = `capture-run:${"a".repeat(64)}`;
const BT_RUN = "backtest:test:truth:1";
const ISSUER_ID = "issuer:cik:0000320193";

__setApiFetch(async (input) => {
  const url = String(input);
  if (url.includes("/api/v1/admin/ops")) {
    const payload =
      mode === "empty"
        ? { runs: [], app_git_sha: "deadbeef1234", dependencies: [], served_heads: [], nightly_verdicts: [] }
        : {
            app_git_sha: "abc123def456",
            data_engine: { git_sha: "fed987654321", image_digest: "sha256:1111", run_id: "de:1", created_at: "2026-11-25T00:00:00Z" },
            dependencies: [{ name: "database", status: "present", elapsed_ms: 2.4, detail: "Postgres ops reader reachable" }],
            served_heads: [
              {
                environment: "staging",
                universe_id: "universe:topt-us-v1",
                universe_version: "2026-11-01",
                factor_id: "gross_profit_per_employee",
                sequence: 4,
                head_run_id: RUN_ID,
                run_id: RUN_ID,
                age_hours: 14.5,
                limit_hours: 72.0,
                freshness: "fresh",
                availability: "available",
                staleness_reason: null,
              },
            ],
            nightly_verdicts: [
              { check_name: "output_invariants", ok: true, freshness_state: "pass", age_hours: 2.0, max_age_hours: 48.0, ran_at: "2026-11-25T01:00:00Z", summary: "all hard checks clean", dagster_run_id: "dag-1" },
              { check_name: "topt_baseline", ok: true, freshness_state: "stale", age_hours: 72.0, max_age_hours: 48.0, ran_at: "2026-11-22T01:00:00Z", summary: "overdue check", dagster_run_id: "dag-0" },
              { check_name: "model_key_health", ok: null, freshness_state: "missing", age_hours: null, max_age_hours: 48.0, ran_at: null, summary: "missing from mart.nightly_verdicts", dagster_run_id: null },
            ],
            construction_progress: {
              environment: "staging",
              criteria_total: 30,
              criteria_verified: 6,
              criteria_partial: 14,
              criteria_pending: 8,
              criteria_failed_or_stale: 2,
              criteria: [],
              universes: [
                {
                  universe_key: "topt",
                  universe_id: "universe:topt-us-v1",
                  scheduled: true,
                  freshness: "fresh",
                  availability: "available",
                  report_matches_head: true,
                  questions: [
                    { question_id: "q1", answered: 8, denominator: 10, actionable_denominator: 9, actionable_coverage_pct: 88.89, world_absence: 1, pipeline_gap: 1, not_applicable: 0 },
                  ],
                },
              ],
              strategy_runs: [
                { strategy_key: "large_model_value_v0", strategy_run_id: "strategy-run:" + "c".repeat(64), executed_at: "2026-11-25T02:00:00Z", selected_count: 3, eligible_count: 5, total_count: 10 },
              ],
              backtest_runs: [
                { strategy_key: "large_model_value_v0", universe_id: "universe:topt-us-v1", status: "succeeded", run_id: BT_RUN, executed_at: "2026-11-25T03:00:00Z", cagr_monthly: "0.12", sharpe_daily: "1.1" },
              ],
            },
            failed_job_errors: [{ run_id: "dag-err-1", logged_at: "2026-11-25T00:30:00Z", first_line_error: "Upstream timeout 504" }],
            runs: [{ run_id: "dag-1", job_name: "topt_live_pipeline", status: "SUCCESS", created_at: "2026-11-25T00:00:00Z", duration_seconds: 90 }],
            trigger_queue: [{ request_id: 11, job_name: "sp500_live_pipeline", force_fetch: true, requested_at: "2026-11-25T00:10:00Z", consumed_at: null, launched_run_key: null }],
            rollback_requests: [{ request_id: 3, universe_id: "universe:topt-us-v1", target_run_id: RUN_ID, reason: "revert spike", consumed_at: null, result_pointer_id: null }],
          };
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes("/api/v1/admin/datahub")) {
    const payload =
      mode === "empty"
        ? { sources: [], capture_runs: [], validation: [], capacity: [], traffic: [], recent_calls: [], ledger_coverage: [], question_coverage: [], confidence: [], oracle_outcomes: [] }
        : {
            sources: [{ source: "sec", fetches_total: 100, fetches_24h: 12, last_fetch: "2026-11-25T00:00:00Z" }],
            capture_runs: [{ run_id: RUN_ID, universe_id: "universe:topt-us-v1", cutoff: "2026-11-25", obligations: 100, resolved: 99, failed: 1, complete: true }],
            question_coverage: [
              {
                universe_id: "universe:topt-us-v1",
                report_run_id: RUN_ID,
                served_head_run_id: RUN_ID,
                matches_served_head: true,
                cutoff: "2026-11-25",
                created_at: "2026-11-25T01:00:00Z",
                payload: {
                  previous_run_id: `capture-run:${"b".repeat(64)}`,
                  questions: {
                    q1: {
                      answered: 95,
                      denominator: 100,
                      not_applicable: 5,
                      by_class: { world_absence: 3, transient: 2 },
                      violations: { no_row: 0 },
                      change: { answered: 2, subjects_changed: 2, examples: [{ subject: "AAPL" }, { subject: "MSFT" }] },
                    },
                  },
                },
              },
            ],
            oracle_outcomes: [
              { universe_id: "universe:topt-us-v1", field: "revenue", compared: 10, agree: 9, disagree: 1, not_comparable: 0, no_served_value_count: 0, wilson_95_upper_bound: "0.4000", newest_compared_at: "2026-11-25T00:00:00Z" },
              { universe_id: "universe:topt-us-v1", field: "custom_extra_metric", compared: 5, agree: 5, disagree: 0, not_comparable: 0, no_served_value_count: 0, wilson_95_upper_bound: "0.4300", newest_compared_at: "2026-11-25T00:00:00Z" },
            ],
            oracle_disagreements: [
              { universe_id: "universe:topt-us-v1", subject_id: ISSUER_ID, field: "revenue", period: "2025-12-31", oracle_kind: "filing", source: "sec", served_value: "100", oracle_value: "110", ratio: "0.09", tolerance: "0.02", reason: "mismatch", compared_at: "2026-11-25T00:00:00Z" },
            ],
            served_heads: [
              { universe_id: "universe:topt-us-v1", universe_version: "2026-11-01", factor_id: "gross_profit_per_employee", sequence: 4, freshness: "fresh", availability: "available", age_hours: 14.5, limit_hours: 72.0, run_id: RUN_ID, head_run_id: RUN_ID, served_to: "/research/rankings", bound_strategy_run_id: "strategy-run:" + "c".repeat(64), staleness_reason: null },
            ],
            served_artifacts: [
              { artifact_key: "topt_gppe", lane: "capture", schedule_name: "topt_daily", universe_like: "universe:topt-%", family: "daily_capture", served_to: "/research/rankings", wired: true },
            ],
            output_invariants: [
              { invariant_id: "selected-weights-close", kind: "hard", population: 20, violation_count: 0, deferred: false, sample_rows: [], checked_at: "2026-11-25T01:00:00Z" },
            ],
            validation: [{ check: "plausibility sign checks", verdict: "pass", detail: "0 implausible cells" }],
            nightly_verdicts: [{ check_name: "output_invariants", ok: true, ran_at: "2026-11-25T01:00:00Z", summary: "ok", dagster_run_id: "dag-1" }],
            confidence: [
              {
                universe_id: "universe:topt-us-v1",
                cutoff: "2026-11-25",
                run_id: RUN_ID,
                created_at: "2026-11-25T01:00:00Z",
                payload: {
                  families: {
                    close: {
                      cells: 5,
                      mean_confidence: "0.9100",
                      by_independent_origins: { "1": 2, "2": 3 },
                      agreement_rate: "1.0000",
                    },
                  },
                  cells: {
                    close: {
                      "issuer:a": { tier: "High", confidence: "0.95" },
                      "issuer:b": { tier: "Medium", confidence: "0.70" },
                    },
                  },
                },
              },
            ],
            capacity: [{ account: "twelvedata", environment: "staging", limit_id: "per_day", window_seconds: 86400, share: 320, observed_calls: 64, remaining_estimate: 256, utilization_pct: 20.0, rate_limited_24h: 0 }],
            model_1302_by_day: [{ day: "2026-11-25", source: "zhipu", status_429: 1, code_1302: 2, total_calls: 20, failed_calls: 3 }],
            llm_usage: [{ day: "2026-11-25", provider: "zhipu", served_model: "glm-4.5", standard: "gppe", calls: 15, prompt_tokens: 1200, completion_tokens: 400, cost: "0.25", error_calls: 1, refusal_calls: 0 }],
            traffic: [{ source: "sec", calls: 40, failed: 0, avg_ms: 120, last_call: "2026-11-25 00:00:00Z" }],
            recent_calls: [{ id: 1, called_at: "2026-11-25 00:00:00Z", source: "sec", endpoint: "/api/xbrl", caller: "capture", ok: true, status_code: 200, duration_ms: 110, run_key: "rk-1", error: null }],
            ledger_coverage: [{ day: "2026-11-25", source: "sec", endpoint: "/api/xbrl", calls: 40, no_key: 0, by_reason: {}, keys_twice: 0, run_share: "1.0000", calls_per_run: "40.0" }],
            backfill_health: [{ id: 1, source: "sec", metric: "backfill_lag_hours", value: "0", logged_at: "2026-11-25T00:00:00Z", note: "healthy" }],
            trigger_queue: [{ request_id: 11, job_name: "qqq_live_pipeline", force_fetch: false, requested_at: "2026-11-25T00:10:00Z", consumed_at: "2026-11-25T00:10:25Z", launched_run_key: "manual:d1", requested_by: "principal:admin" }],
            rollback_requests: [{ request_id: 3, universe_id: "universe:topt-us-v1", target_run_id: RUN_ID, reason: "revert spike", requested_at: "2026-11-25T00:20:00Z", consumed_at: null, result_pointer_id: null }],
            db_hygiene: {
              stats_reset: "2026-11-25T00:00:00Z",
              zero_scan_indexes: [{ schemaname: "mart", table_name: "demo_tbl", index_name: "idx_demo_unused", size_bytes: 16384 }],
              idle_tables: [{ schemaname: "app", table_name: "conversations", n_live_tup: 0, total_scans: 0, size_bytes: 8192 }],
            },
          };
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes("/api/v1/panel/coverage")) {
    return new Response(
      JSON.stringify({
        available: true,
        governed: true,
        head_age_hours: 18.0,
        head_limit_hours: 72.0,
        head_freshness: "fresh",
        cutoff_at: "2026-11-24T22:15:00Z",
        decisions: [],
        wide_panel: {},
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes("/api/v1/panel/universes")) {
    return new Response(JSON.stringify({ heads: [], members: {} }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes("/api/v1/panel/macro")) {
    return new Response(
      JSON.stringify({
        available: true,
        series: [{ series_id: "DGS10", label: "10Y Treasury", unit: "annualized_decimal_fraction", is_fresh: true, latest: { value: "0.042", observation_date: "2026-11-24", source: "treasury", fetched_at: "2026-11-24T22:00:00Z" } }],
        months: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes("/api/v1/strategy/rankings")) {
    return new Response(
      JSON.stringify({
        available: true,
        governed: true,
        head_age_hours: 18.0,
        head_limit_hours: 72.0,
        head_freshness: "fresh",
        definition_version: "0.1.0",
        review_state: "reviewed",
        cutoff_at: "2026-11-24T22:15:00Z",
        rows: [
          {
            issuer_id: ISSUER_ID,
            label: "AAPL · APPLE",
            cutoff_at: "2026-11-24T22:15:00Z",
            outcome: "selected",
            eligible: true,
            trace_id: "trace:aapl",
            rank: 1,
            tier: "tech",
            availability: "low_confidence",
            source_evidence_status: "degraded",
            factor_validation_status: "accepted",
            peg: null,
            peg_reason_codes: ["non_positive_growth"],
            gppe_variants: {},
            panel_inputs: {},
            provenance: {},
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes("/api/v1/entities/themes")) {
    return new Response(
      JSON.stringify({
        unvisited_count: 7,
        groups: [
          {
            theme_id: "ai-infra",
            theme: "AI Infrastructure",
            definition_version: "v1",
            definition_sha256: "e".repeat(64),
            run_id: RUN_ID,
            cutoff: "2026-11-24",
            rows: [
              {
                issuer_id: ISSUER_ID,
                label: "NVDA",
                theme_share: "0.80",
                in_theme_revenue: "8000000",
                consolidated_revenue: "10000000",
                segments: 2,
                confidence: "0.90",
                period_end: "2026-09-30",
                availability_status: "low_confidence",
                source_evidence_status: "verified",
                factor_validation_status: "accepted",
              },
            ],
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes(`/api/v1/entities/${encodeURIComponent(ISSUER_ID)}/supply-chain`)) {
    return new Response(JSON.stringify({ reason: "unavailable" }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes(`/api/v1/entities/${encodeURIComponent(ISSUER_ID)}/analyst-ratings`)) {
    return new Response(JSON.stringify({ reason: "unavailable" }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes(`/api/v1/entities/${encodeURIComponent(ISSUER_ID)}`)) {
    return new Response(
      JSON.stringify({
        issuer_id: ISSUER_ID,
        label: "AAPL · APPLE",
        basic_info: {
          canonical_name: "Apple Inc.",
          primary_ticker: "AAPL",
          primary_exchange: "XNAS",
          cik: "0000320193",
          sic: "3571",
          sic_description: "Electronic Computers",
          sector_gics_or_sic: "Electronic Computers",
          fiscal_year_end: "0930",
          state_of_incorporation: "CA",
          entity_type: "operating",
          market_cap: "3000000000000",
          market_cap_confidence: "0.92",
          shares_outstanding: "15000000000",
          close_price: "200",
          as_of_cutoff: "2026-11-24",
        },
        decisions: [
          {
            issuer_id: ISSUER_ID,
            cutoff_at: "2026-11-24T22:15:00Z",
            rank: 1,
            tier: "tech",
            availability: "low_confidence",
            source_evidence_status: "degraded",
            factor_validation_status: "accepted",
            peg: null,
            peg_reason_codes: ["insufficient_earnings_history"],
          },
        ],
        themes: [
          {
            theme_id: "ai-infra",
            theme: "AI Infrastructure",
            theme_share: "0.45",
            in_theme_revenue: "45000000",
            consolidated_revenue: "100000000",
            segments: 3,
            confidence: "0.88",
            availability_status: "available",
          },
        ],
        node_values: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes(`/api/v1/strategy/backtests/${encodeURIComponent(BT_RUN)}`)) {
    return new Response(
      JSON.stringify({
        run: {
          run_id: BT_RUN,
          strategy_key: "large_model_value_v0",
          strategy_version: "0.1.0",
          universe_id: "universe:topt-us-v1",
          start_date: "2025-01-01",
          end_date: "2026-01-01",
          status: "failed",
          error_message: "Insufficient price history for backtest window",
          created_at: "2026-11-25T00:00:00Z",
          executed_at: "2026-11-25T00:00:00Z",
        },
        valuations: [],
        trades: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  return new Response(JSON.stringify({ detail: `unmatched ${url}` }), { status: 404 });
});

try {
  // 1. Rich AdminOps + AdminDatahub
  mode = "rich";
  const opsRich = renderToStaticMarkup(await AdminOpsPage());
  assertCleanHtml(opsRich, "AdminOpsPage(rich)");
  for (const needle of [
    "Dependencies",
    "Postgres ops reader reachable",
    "Served heads",
    "Nightly verdicts",
    "STALE",
    "MISSING",
    "Construction &amp; 6-Scenario Progress Matrix",
    "selected 3 / eligible 5 / total 10",
    "Failed job errors",
    "Upstream timeout 504",
    "Head rollback",
    "Target universe",
  ]) {
    assert(opsRich.includes(needle), `AdminOpsPage missing ${needle}`);
  }

  const dhRich = renderToStaticMarkup(await AdminDatahubPage());
  assertCleanHtml(dhRich, "AdminDatahubPage(rich)");
  for (const needle of [
    "Question coverage matrix",
    "Aligned",
    "Question coverage detail (layers &amp; deltas)",
    "95/100",
    "+2 (2: AAPL, MSFT)",
    "world_absence=3",
    "custom_extra_metric",
    "0.9100",
    "1 / 1 / 0",
    "2 / 3",
    "1.0000",
    "Oracle disagreements (sample)",
    "Served heads &amp; pipeline artifacts",
    "topt_gppe",
    "Output invariants &amp; validation",
    "selected-weights-close",
    "Capacity, Vendor Quotas &amp; 429/1302",
    "LLM usage &amp; model drift",
    "glm-4.5",
    "Backfill health &amp; queues",
    "backfill_lag_hours",
    "DB hygiene facts",
    "idx_demo_unused",
    "conversations",
  ]) {
    assert(dhRich.includes(needle), `AdminDatahubPage missing ${needle}`);
  }

  // 2. Empty AdminOps + AdminDatahub (zero leakage)
  mode = "empty";
  assertCleanHtml(renderToStaticMarkup(await AdminOpsPage()), "AdminOpsPage(empty)");
  assertCleanHtml(renderToStaticMarkup(await AdminDatahubPage()), "AdminDatahubPage(empty)");

  // 3. Research pages
  const covHtml = renderToStaticMarkup(await CoveragePage());
  assertCleanHtml(covHtml, "CoveragePage");
  assert(covHtml.includes("18.00h / 72.00h · fresh"), "CoveragePage renders backend head_freshness");
  assert(covHtml.includes(">fresh<"), "CoveragePage renders macro freshness badge");

  const rankHtml = renderToStaticMarkup(await RankingsPage({ searchParams: Promise.resolve({}) }));
  assertCleanHtml(rankHtml, "RankingsPage");
  assert(rankHtml.includes("review: reviewed"), "RankingsPage ClaimCeilingBanner renders review_state");
  assert(rankHtml.includes("degraded / accepted"), "RankingsPage renders 3-dim statuses");
  assert(rankHtml.includes("non_positive_growth"), "RankingsPage renders peg_reason_codes");

  const themesHtml = renderToStaticMarkup(await ThemesPage());
  assertCleanHtml(themesHtml, "ThemesPage");
  assert(themesHtml.includes("Unvisited theme rows (no segment partition):"), "ThemesPage renders unvisited_count");
  assert(themesHtml.includes("verified / accepted"), "ThemesPage renders 3-dim statuses");

  const entityHtml = renderToStaticMarkup(
    await EntityDetailPage({ params: Promise.resolve({ id: ISSUER_ID }), searchParams: Promise.resolve({}) }),
  );
  assertCleanHtml(entityHtml, "EntityDetailPage");
  assert(entityHtml.includes('data-testid="entity-basic-info"') && entityHtml.includes("Electronic Computers") && entityHtml.includes("0.92"), "EntityDetailPage renders Stock Basic Profile");
  assert(entityHtml.includes('data-testid="entity-themes-section"') && entityHtml.includes("AI Infrastructure"), "EntityDetailPage renders themes");
  assert(entityHtml.includes("insufficient_earnings_history"), "EntityDetailPage renders peg_reason_codes");

  const btHtml = renderToStaticMarkup(await BacktestDetailPage({ params: Promise.resolve({ id: BT_RUN }) }));
  assertCleanHtml(btHtml, "BacktestDetailPage");
  assert(btHtml.includes("Insufficient price history for backtest window"), "BacktestDetailPage renders error_message");

  console.log("admin-truth-dashboards: all 7 Wave 2 dashboards & research truth pages verified");
} finally {
  __setApiFetch(null);
}
