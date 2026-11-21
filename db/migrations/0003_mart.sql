-- idempotent: true
-- Canonical mart schema baseline (Wave 2 squash).
set lock_timeout = '5s';
set statement_timeout = '30s';

create schema if not exists mart;

create table if not exists mart.backtest_runs (
 run_id text not null,
 strategy_key text not null,
 strategy_version text not null,
 universe_id text not null,
 start_date date not null,
 end_date date not null,
 status text not null,
 cagr_monthly numeric,
 sharpe_daily numeric,
 max_dd_daily numeric,
 vol_daily numeric,
 turnover_monthly numeric,
 calmar_daily numeric,
 metrics_payload jsonb default '{}'::jsonb not null,
 error_message text,
 executed_at timestamp with time zone not null,
 created_at timestamp with time zone default now() not null,
 definition_content_sha256 text,
 price_basis text,
 claim_ceiling text,
 benchmark_rule text,
 benchmark_cagr numeric,
 benchmark_max_dd numeric,
 constraint backtest_runs_pkey PRIMARY KEY (run_id),
 constraint backtest_runs_definition_sha256_shape CHECK (((definition_content_sha256 IS NULL) OR (definition_content_sha256 ~ '^[0-9a-f]{64}$'::text))),
 constraint backtest_runs_run_id_check CHECK ((run_id ~ '^backtest-run:[0-9a-f]{64}$'::text)),
 constraint backtest_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'succeeded'::text, 'failed'::text])))
);

create table if not exists mart.cell_confidence (
 cell_confidence_id bigserial,
 run_id text not null,
 subject_id text not null,
 semantic_type text not null,
 metric text not null,
 observation_id text,
 confidence numeric(10,6) not null,
 policy_hash text not null,
 input_hash text not null,
 reason_codes text[] default '{}'::text[] not null,
 recorded_at timestamp with time zone default now() not null,
 constraint cell_confidence_pkey PRIMARY KEY (cell_confidence_id),
 constraint cell_confidence_run_subject_metric_unique UNIQUE (run_id, subject_id, metric),
 constraint cell_confidence_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);

create table if not exists mart.current_pointer (
 pointer_id text not null,
 content_sha256 text not null,
 environment text not null,
 universe_id text not null,
 universe_version text not null,
 factor_id text not null,
 target_run_id text not null,
 sequence bigint not null,
 previous_run_id text,
 advanced_at timestamp with time zone not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint current_pointer_pkey PRIMARY KEY (pointer_id),
 constraint current_pointer_unique_advance UNIQUE (environment, universe_id, universe_version, factor_id, sequence),
 constraint current_pointer_advance_changes_run CHECK (((previous_run_id IS NULL) OR (previous_run_id <> target_run_id))),
 constraint current_pointer_first_has_no_previous CHECK (((sequence = 0) = (previous_run_id IS NULL))),
 constraint current_pointer_hash_matches_id CHECK ((split_part(pointer_id, ':'::text, 2) = content_sha256)),
 constraint current_pointer_id_shape CHECK ((pointer_id ~ '^current-pointer:[0-9a-f]{64}$'::text)),
 constraint current_pointer_previous_is_run CHECK (((previous_run_id IS NULL) OR (split_part(previous_run_id, ':'::text, 1) = 'capture-run'::text))),
 constraint current_pointer_sequence_nonneg CHECK ((sequence >= 0)),
 constraint current_pointer_target_is_run CHECK ((split_part(target_run_id, ':'::text, 1) = 'capture-run'::text)),
 constraint current_pointer_previous_run_id_fkey FOREIGN KEY (previous_run_id) REFERENCES staging.evidence_nodes(node_id),
 constraint current_pointer_target_run_id_fkey FOREIGN KEY (target_run_id) REFERENCES staging.evidence_nodes(node_id)
);

create table if not exists mart.datahub_confidence_report (
 report_id text not null,
 content_sha256 text not null,
 universe_id text not null,
 run_id text not null,
 cutoff timestamp with time zone not null,
 payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint datahub_confidence_report_pkey PRIMARY KEY (report_id),
 constraint datahub_confidence_report_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint datahub_confidence_report_hash_matches_id CHECK ((split_part(report_id, ':'::text, 2) = content_sha256)),
 constraint datahub_confidence_report_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint datahub_confidence_report_report_id_check CHECK ((report_id ~ '^datahub-confidence-report:[0-9a-f]{64}$'::text)),
 constraint datahub_confidence_report_run_id_check CHECK ((run_id ~ '^capture-run:[0-9a-f]{64}$'::text))
);

create table if not exists mart.datahub_quality_report (
 report_id text not null,
 content_sha256 text not null,
 run_id text not null,
 requested_count integer not null,
 payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint datahub_quality_report_pkey PRIMARY KEY (report_id),
 constraint datahub_quality_report_hash_matches_id CHECK ((split_part(report_id, ':'::text, 2) = content_sha256)),
 constraint datahub_quality_report_id_shape CHECK ((report_id ~ '^datahub-quality-report:[0-9a-f]{64}$'::text))
);

create table if not exists mart.environment_identity (
 singleton boolean default true not null,
 environment text not null,
 declared_at timestamp with time zone default clock_timestamp() not null,
 constraint environment_identity_pkey PRIMARY KEY (singleton),
 constraint environment_identity_singleton_check CHECK (singleton)
);

create table if not exists mart.freshness_limit (
 limit_key text not null,
 hours integer not null,
 constraint freshness_limit_pkey PRIMARY KEY (limit_key),
 constraint freshness_limit_hours_check CHECK (((hours >= 1) AND (hours <= 720)))
);

create table if not exists mart.fund_virtual_company (
 run_id text not null,
 fund_id text not null,
 fund_name text default ''::text not null,
 report_period date not null,
 transaction_time timestamp with time zone not null,
 cutoff timestamp with time zone not null,
 definition_version text not null,
 definition_sha256 text not null,
 weighted_valuation_gap numeric,
 total_weight_pct numeric not null,
 resolved_weight_pct numeric not null,
 valued_weight_pct numeric not null,
 lines integer not null,
 valued_lines integer not null,
 confidence numeric not null,
 reason_codes text[] default '{}'::text[] not null,
 availability_status text not null,
 source_evidence_status text not null,
 factor_validation_status text not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 valuation_basis text,
 constraint fund_virtual_company_pkey PRIMARY KEY (run_id, fund_id),
 constraint chk_fund_virtual_company_weight_conservation CHECK (((total_weight_pct >= (0)::numeric) AND (total_weight_pct <= (105)::numeric) AND (resolved_weight_pct >= (0)::numeric) AND (resolved_weight_pct <= total_weight_pct) AND (valued_weight_pct >= (0)::numeric) AND (valued_weight_pct <= resolved_weight_pct))),
 constraint fund_virtual_company_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'error'::text]))),
 constraint fund_virtual_company_check CHECK ((valued_weight_pct <= resolved_weight_pct)),
 constraint fund_virtual_company_check1 CHECK ((resolved_weight_pct <= total_weight_pct)),
 constraint fund_virtual_company_check2 CHECK ((valued_lines <= lines)),
 constraint fund_virtual_company_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint fund_virtual_company_definition_sha256_check CHECK ((definition_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint fund_virtual_company_definition_version_check CHECK ((definition_version <> ''::text)),
 constraint fund_virtual_company_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint fund_virtual_company_lines_check CHECK ((lines >= 0)),
 constraint fund_virtual_company_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text]))),
 constraint fund_virtual_company_valued_lines_check CHECK ((valued_lines >= 0))
);

create table if not exists mart.issuer_analyst_ratings (
 run_id text not null,
 issuer_id text not null,
 cutoff timestamp with time zone not null,
 consensus_rating numeric,
 analysts_count integer default 0 not null,
 buy_count integer default 0 not null,
 hold_count integer default 0 not null,
 sell_count integer default 0 not null,
 confidence numeric default 0 not null,
 reason_codes text[] default '{}'::text[] not null,
 extractor text default 'origin:moomoo:v1'::text not null,
 availability_status text not null,
 source_evidence_status text not null,
 factor_validation_status text not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint issuer_analyst_ratings_pkey PRIMARY KEY (run_id, issuer_id),
 constraint issuer_analyst_ratings_analysts_count_check CHECK ((analysts_count >= 0)),
 constraint issuer_analyst_ratings_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'error'::text]))),
 constraint issuer_analyst_ratings_buy_count_check CHECK ((buy_count >= 0)),
 constraint issuer_analyst_ratings_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint issuer_analyst_ratings_consensus_rating_check CHECK (((consensus_rating IS NULL) OR ((consensus_rating >= (1)::numeric) AND (consensus_rating <= (5)::numeric)))),
 constraint issuer_analyst_ratings_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint issuer_analyst_ratings_hold_count_check CHECK ((hold_count >= 0)),
 constraint issuer_analyst_ratings_issuer_id_check CHECK ((issuer_id <> ''::text)),
 constraint issuer_analyst_ratings_sell_count_check CHECK ((sell_count >= 0)),
 constraint issuer_analyst_ratings_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text])))
);

create table if not exists mart.issuer_supply_chain_exposure (
 run_id text not null,
 issuer_id text not null,
 cutoff timestamp with time zone not null,
 exposure_score numeric,
 direct_partners integer default 0 not null,
 suppliers_count integer default 0 not null,
 customers_count integer default 0 not null,
 max_partner_share numeric default 0 not null,
 confidence numeric default 0 not null,
 reason_codes text[] default '{}'::text[] not null,
 extractor text default 'graph:entity-relations:v1'::text not null,
 availability_status text not null,
 source_evidence_status text not null,
 factor_validation_status text not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint issuer_supply_chain_exposure_pkey PRIMARY KEY (run_id, issuer_id),
 constraint issuer_supply_chain_exposure_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'error'::text]))),
 constraint issuer_supply_chain_exposure_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint issuer_supply_chain_exposure_customers_count_check CHECK ((customers_count >= 0)),
 constraint issuer_supply_chain_exposure_direct_partners_check CHECK ((direct_partners >= 0)),
 constraint issuer_supply_chain_exposure_exposure_score_check CHECK (((exposure_score IS NULL) OR ((exposure_score >= (0)::numeric) AND (exposure_score <= (1)::numeric)))),
 constraint issuer_supply_chain_exposure_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint issuer_supply_chain_exposure_issuer_id_check CHECK ((issuer_id <> ''::text)),
 constraint issuer_supply_chain_exposure_max_partner_share_check CHECK (((max_partner_share >= (0)::numeric) AND (max_partner_share <= (1)::numeric))),
 constraint issuer_supply_chain_exposure_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text]))),
 constraint issuer_supply_chain_exposure_suppliers_count_check CHECK ((suppliers_count >= 0))
);

create table if not exists mart.issuer_theme_purity (
 run_id text not null,
 issuer_id text not null,
 cik integer,
 theme_id text not null,
 theme text not null,
 definition_version text not null,
 definition_sha256 text not null,
 cutoff timestamp with time zone not null,
 period_end date,
 partition_id text,
 theme_share numeric,
 consolidated_revenue numeric,
 in_theme_revenue numeric,
 out_of_theme_revenue numeric,
 unclassified_revenue numeric,
 partition_residual numeric,
 segments integer not null,
 confidence numeric not null,
 reason_codes text[] default '{}'::text[] not null,
 extractor text not null,
 availability_status text not null,
 source_evidence_status text not null,
 factor_validation_status text not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint issuer_theme_purity_pkey PRIMARY KEY (run_id, issuer_id, theme_id),
 constraint issuer_theme_purity_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'error'::text]))),
 constraint issuer_theme_purity_check CHECK (((((in_theme_revenue + out_of_theme_revenue) + unclassified_revenue) + partition_residual) = consolidated_revenue)),
 constraint issuer_theme_purity_cik_check CHECK ((cik > 0)),
 constraint issuer_theme_purity_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint issuer_theme_purity_consolidated_revenue_check CHECK ((consolidated_revenue > (0)::numeric)),
 constraint issuer_theme_purity_definition_sha256_check CHECK ((definition_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint issuer_theme_purity_definition_version_check CHECK ((definition_version <> ''::text)),
 constraint issuer_theme_purity_extractor_check CHECK ((extractor <> ''::text)),
 constraint issuer_theme_purity_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint issuer_theme_purity_issuer_id_check CHECK ((issuer_id <> ''::text)),
 constraint issuer_theme_purity_partition_id_check CHECK ((partition_id ~ '^segment-partition:[0-9a-f]{64}$'::text)),
 constraint issuer_theme_purity_partition_or_fill_check CHECK ((((extractor = 'lane:unvisited:v1'::text) AND (availability_status = 'unavailable'::text) AND (theme_share IS NULL) AND (cik IS NULL) AND (period_end IS NULL) AND (partition_id IS NULL) AND (consolidated_revenue IS NULL) AND (in_theme_revenue IS NULL) AND (out_of_theme_revenue IS NULL) AND (unclassified_revenue IS NULL) AND (partition_residual IS NULL)) OR ((extractor <> 'lane:unvisited:v1'::text) AND (cik IS NOT NULL) AND (period_end IS NOT NULL) AND (partition_id IS NOT NULL) AND (consolidated_revenue IS NOT NULL) AND (in_theme_revenue IS NOT NULL) AND (out_of_theme_revenue IS NOT NULL) AND (unclassified_revenue IS NOT NULL) AND (partition_residual IS NOT NULL)))),
 constraint issuer_theme_purity_segments_check CHECK ((segments >= 0)),
 constraint issuer_theme_purity_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text]))),
 constraint issuer_theme_purity_theme_check CHECK ((theme <> ''::text)),
 constraint issuer_theme_purity_theme_id_check CHECK ((theme_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'::text)),
 constraint issuer_theme_purity_theme_share_check CHECK (((theme_share IS NULL) OR ((theme_share >= (0)::numeric) AND (theme_share <= (1)::numeric))))
);

create table if not exists mart.macro_series (
 series_id text not null,
 label text not null,
 unit text not null,
 source_id text not null,
 max_age_days integer not null,
 updated_at timestamp with time zone default clock_timestamp() not null,
 constraint macro_series_pkey PRIMARY KEY (series_id),
 constraint macro_series_label_check CHECK ((label <> ''::text)),
 constraint macro_series_max_age_days_check CHECK ((max_age_days > 0)),
 constraint macro_series_series_id_check CHECK ((series_id ~ '^[a-z0-9_]+$'::text)),
 constraint macro_series_source_id_check CHECK ((source_id <> ''::text)),
 constraint macro_series_unit_check CHECK ((unit <> ''::text))
);

create table if not exists mart.metric_node_values (
 run_id text not null,
 issuer_id text not null,
 node_id uuid not null,
 node_key text not null,
 period text not null,
 period_tag text,
 value numeric,
 unit text,
 availability_status text not null,
 reason_codes text[] default '{}'::text[] not null,
 source_evidence_status text not null,
 factor_validation_status text not null,
 confidence numeric,
 confidence_band text,
 tree_id uuid,
 tree_version text,
 tree_sha256 text,
 input_observation_ids text[] default '{}'::text[] not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint metric_node_values_pkey PRIMARY KEY (run_id, issuer_id, node_id, period),
 constraint metric_node_values_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'not_applicable'::text, 'error'::text]))),
 constraint metric_node_values_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))),
 constraint metric_node_values_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint metric_node_values_issuer_id_check CHECK ((issuer_id <> ''::text)),
 constraint metric_node_values_node_key_check CHECK ((node_key <> ''::text)),
 constraint metric_node_values_period_check CHECK ((period <> ''::text)),
 constraint metric_node_values_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text]))),
 constraint metric_node_values_tree_sha256_check CHECK (((tree_sha256 IS NULL) OR (tree_sha256 ~ '^[0-9a-f]{64}$'::text)))
);

create table if not exists mart.nightly_verdicts (
 verdict_id bigint generated always as identity,
 check_name text not null,
 ran_at timestamp with time zone not null,
 ok boolean,
 summary text not null,
 dagster_run_id text not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint nightly_verdicts_pkey PRIMARY KEY (verdict_id),
 constraint nightly_verdicts_check_name_check CHECK ((check_name ~ '^[a-z0-9_]+(@[a-z0-9_.:-]+)?$'::text)),
 constraint nightly_verdicts_dagster_run_id_check CHECK (((char_length(dagster_run_id) >= 1) AND (char_length(dagster_run_id) <= 64))),
 constraint nightly_verdicts_summary_check CHECK (((char_length(summary) >= 1) AND (char_length(summary) <= 300)))
);

create table if not exists mart.oracle_outcomes (
 outcome_id bigint generated always as identity,
 run_id text not null,
 subject_id text not null,
 field text not null,
 period text not null,
 served_value numeric,
 oracle_value numeric,
 oracle_kind text not null,
 source text not null,
 observation_id text,
 knowable_at timestamp with time zone,
 ratio numeric,
 tolerance numeric,
 result text not null,
 reason text,
 compared_at timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint oracle_outcomes_pkey PRIMARY KEY (outcome_id),
 constraint oracle_outcomes_once UNIQUE (run_id, subject_id, field, period, oracle_kind),
 constraint oracle_outcomes_compared_carries_its_numbers CHECK (((result = 'not_comparable'::text) OR ((served_value IS NOT NULL) AND (oracle_value IS NOT NULL) AND (ratio IS NOT NULL) AND (tolerance IS NOT NULL) AND (knowable_at IS NOT NULL) AND (observation_id IS NOT NULL)))),
 constraint oracle_outcomes_field_check CHECK ((field <> ''::text)),
 constraint oracle_outcomes_not_comparable_says_why CHECK (((result <> 'not_comparable'::text) OR (reason IS NOT NULL))),
 constraint oracle_outcomes_observation_id_check CHECK (((observation_id IS NULL) OR (observation_id <> ''::text))),
 constraint oracle_outcomes_oracle_kind CHECK ((oracle_kind = ANY (ARRAY['filing'::text, 'independent_parse'::text, 'operator'::text]))),
 constraint oracle_outcomes_oracle_value_check CHECK (((oracle_value IS NULL) OR (oracle_value <> 'NaN'::numeric))),
 constraint oracle_outcomes_period_check CHECK ((period <> ''::text)),
 constraint oracle_outcomes_ratio CHECK (((ratio IS NULL) OR ((ratio >= (0)::numeric) AND (ratio <> 'NaN'::numeric)))),
 constraint oracle_outcomes_reason CHECK (((reason IS NULL) OR (reason <> ''::text))),
 constraint oracle_outcomes_result CHECK ((result = ANY (ARRAY['agree'::text, 'disagree'::text, 'not_comparable'::text]))),
 constraint oracle_outcomes_run_id_check CHECK ((run_id <> ''::text)),
 constraint oracle_outcomes_served_value_check CHECK (((served_value IS NULL) OR (served_value <> 'NaN'::numeric))),
 constraint oracle_outcomes_source_check CHECK ((source <> ''::text)),
 constraint oracle_outcomes_subject_id_check CHECK ((subject_id <> ''::text)),
 constraint oracle_outcomes_tolerance CHECK (((tolerance IS NULL) OR ((tolerance >= (0)::numeric) AND (tolerance <> 'NaN'::numeric))))
);

create table if not exists mart.question_coverage_report (
 report_id text not null,
 content_sha256 text not null,
 universe_id text not null,
 run_id text not null,
 cutoff timestamp with time zone not null,
 requirements_sha256 text not null,
 payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint question_coverage_report_pkey PRIMARY KEY (report_id),
 constraint question_coverage_report_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint question_coverage_report_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint question_coverage_report_report_id_check CHECK ((report_id ~ '^question-coverage-report:[0-9a-f]{64}$'::text)),
 constraint question_coverage_report_requirements_sha256_check CHECK ((requirements_sha256 ~ '^[0-9a-f]{64}$'::text))
);

create table if not exists mart.research_card_artifacts (
 run_id text not null,
 card_id text not null,
 card_kind text not null,
 subject_key text not null,
 convention text not null,
 universe text not null,
 cutoff_at timestamp with time zone not null,
 card_json jsonb not null,
 html text not null,
 render_mode text not null,
 model text,
 model_invocation_ref text,
 prompt_sha256 text not null,
 recorded_at timestamp with time zone default now() not null,
 constraint research_card_artifacts_pkey PRIMARY KEY (run_id, card_kind, subject_key, convention, universe)
);

create table if not exists mart.strategy_input_coverage (
 run_id text not null,
 issuer_id text not null,
 required_count integer not null,
 present_count integer not null,
 missing_keys text[] not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint strategy_input_coverage_pkey PRIMARY KEY (run_id, issuer_id),
 constraint strategy_input_coverage_check CHECK ((present_count <= required_count)),
 constraint strategy_input_coverage_check1 CHECK ((cardinality(missing_keys) = (required_count - present_count))),
 constraint strategy_input_coverage_present_count_check CHECK ((present_count >= 0)),
 constraint strategy_input_coverage_required_count_check CHECK ((required_count > 0))
);

create table if not exists mart.strategy_runs (
 strategy_run_id text not null,
 content_sha256 text not null,
 strategy_key text not null,
 strategy_version text not null,
 definition_content_sha256 text not null,
 corpus_sha256 text not null,
 claim_ceiling text not null,
 executed_at timestamp with time zone not null,
 created_at timestamp with time zone default now() not null,
 snapshot_id text,
 risk_free_series_id text,
 risk_free_value numeric,
 risk_free_observation_date date,
 risk_free_source text,
 risk_free_fetch_id bigint,
 constraint strategy_runs_pkey PRIMARY KEY (strategy_run_id),
 constraint strategy_runs_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint strategy_runs_corpus_sha256_check CHECK ((corpus_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint strategy_runs_definition_content_sha256_check CHECK ((definition_content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint strategy_runs_risk_free_lineage_whole CHECK ((((risk_free_series_id IS NULL) AND (risk_free_value IS NULL) AND (risk_free_observation_date IS NULL) AND (risk_free_source IS NULL) AND (risk_free_fetch_id IS NULL)) OR ((risk_free_series_id IS NOT NULL) AND (risk_free_value IS NOT NULL) AND (risk_free_observation_date IS NOT NULL) AND (risk_free_source IS NOT NULL) AND (risk_free_fetch_id IS NOT NULL) AND ((risk_free_value >= (0)::numeric) AND (risk_free_value <= (1)::numeric))))),
 constraint strategy_runs_snapshot_id_check CHECK (((snapshot_id IS NULL) OR (snapshot_id ~ '^strategy-snapshot:[0-9a-f]{64}$'::text))),
 constraint strategy_runs_strategy_run_id_check CHECK ((strategy_run_id ~ '^strategy-run:[0-9a-f]{64}$'::text))
);

create table if not exists mart.backtest_trades (
 trade_id bigint generated always as identity,
 run_id text not null,
 trade_date date not null,
 symbol text not null,
 side text not null,
 shares numeric not null,
 execution_price numeric not null,
 trade_value numeric not null,
 weight_before numeric not null,
 weight_after numeric not null,
 fee_paid numeric default 0 not null,
 constraint backtest_trades_pkey PRIMARY KEY (run_id, trade_id),
 constraint backtest_trades_side_check CHECK ((side = ANY (ARRAY['BUY'::text, 'SELL'::text]))),
 constraint backtest_trades_run_id_fkey FOREIGN KEY (run_id) REFERENCES mart.backtest_runs(run_id) ON DELETE CASCADE
);

create table if not exists mart.backtest_valuations (
 run_id text not null,
 resolution text not null,
 valuation_date date not null,
 cum_nav numeric not null,
 drawdown numeric not null,
 gross_exposure numeric not null,
 cash_weight numeric not null,
 benchmark_cum_nav numeric,
 constraint backtest_valuations_pkey PRIMARY KEY (run_id, resolution, valuation_date),
 constraint backtest_valuations_resolution_check CHECK ((resolution = ANY (ARRAY['1M'::text, '1D'::text]))),
 constraint backtest_valuations_run_id_fkey FOREIGN KEY (run_id) REFERENCES mart.backtest_runs(run_id) ON DELETE CASCADE
);

create table if not exists mart.served_artifact (
 artifact_key text not null,
 lane text not null,
 schedule_name text,
 universe_like text,
 family text not null,
 served_to text not null,
 wired boolean not null,
 constraint served_artifact_pkey PRIMARY KEY (artifact_key),
 constraint served_artifact_family_is_a_cadence CHECK ((family <> 'withhold'::text)),
 constraint served_artifact_family_fkey FOREIGN KEY (family) REFERENCES mart.freshness_limit(limit_key)
);

create table if not exists mart.macro_series_daily (
 id bigint generated always as identity,
 series_id text not null,
 observation_date date not null,
 value numeric not null,
 knowable_at timestamp with time zone not null,
 source text not null,
 fetch_id bigint not null,
 fetched_at timestamp with time zone not null,
 confidence numeric not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint macro_series_daily_pkey PRIMARY KEY (id),
 constraint macro_series_daily_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint macro_series_daily_knowable_after_the_day CHECK ((knowable_at >= (((observation_date + 1))::timestamp without time zone AT TIME ZONE 'UTC'::text))),
 constraint macro_series_daily_source_check CHECK ((source <> ''::text)),
 constraint macro_series_daily_value_check CHECK ((value <> 'NaN'::numeric)),
 constraint macro_series_daily_series_id_fkey FOREIGN KEY (series_id) REFERENCES mart.macro_series(series_id)
);

create table if not exists mart.strategy_decisions (
 strategy_decision_id text not null,
 content_sha256 text not null,
 strategy_run_id text not null,
 issuer_id text not null,
 cutoff_at timestamp with time zone not null,
 capital_adjusted_labor_efficiency numeric,
 tier text,
 current_price_to_sales numeric,
 target_price_to_sales numeric,
 valuation_gap numeric,
 eligible boolean not null,
 outcome text not null,
 exclusion_reason text,
 rank integer,
 target_weight numeric,
 created_at timestamp with time zone default now() not null,
 peg numeric,
 peg_rank integer,
 availability_status text,
 source_evidence_status text,
 factor_validation_status text,
 peg_reason_codes text[] default '{}'::text[] not null,
 labor_efficiency_metric text,
 current_price_to_gross_profit numeric,
 target_price_to_gross_profit numeric,
 constraint strategy_decisions_pkey PRIMARY KEY (strategy_decision_id),
 constraint strategy_decisions_strategy_run_id_issuer_id_cutoff_at_key UNIQUE (strategy_run_id, issuer_id, cutoff_at),
 constraint strategy_decisions_availability_status_check CHECK ((availability_status = ANY (ARRAY['available'::text, 'unavailable'::text, 'stale'::text, 'excluded'::text, 'low_confidence'::text, 'not_applicable'::text, 'error'::text]))),
 constraint strategy_decisions_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint strategy_decisions_factor_validation_status_check CHECK ((factor_validation_status = ANY (ARRAY['accepted'::text, 'rejected'::text, 'not_evaluated'::text]))),
 constraint strategy_decisions_peg_positive CHECK (((peg IS NULL) OR (peg > (0)::numeric))),
 constraint strategy_decisions_peg_rank_positive CHECK (((peg_rank IS NULL) OR ((peg IS NOT NULL) AND (peg_rank >= 1)))),
 constraint strategy_decisions_source_evidence_status_check CHECK ((source_evidence_status = ANY (ARRAY['verified'::text, 'degraded'::text, 'rejected'::text]))),
 constraint strategy_decisions_strategy_decision_id_check CHECK ((strategy_decision_id ~ '^strategy-decision:[0-9a-f]{64}$'::text)),
 constraint strategy_decisions_strategy_run_id_fkey FOREIGN KEY (strategy_run_id) REFERENCES mart.strategy_runs(strategy_run_id)
);

create table if not exists mart.strategy_run_capture_bindings (
 capture_run_id text not null,
 strategy_run_id text not null,
 bound_by text not null,
 bound_at timestamp with time zone default clock_timestamp() not null,
 constraint strategy_run_capture_bindings_pkey PRIMARY KEY (capture_run_id, strategy_run_id),
 constraint strategy_run_capture_bindings_bound_by_check CHECK ((bound_by = ANY (ARRAY['tick'::text, 'cutoff-backfill'::text]))),
 constraint strategy_run_capture_bindings_capture_run_id_fkey FOREIGN KEY (capture_run_id) REFERENCES raw.capture_runs(run_id),
 constraint strategy_run_capture_bindings_strategy_run_id_fkey FOREIGN KEY (strategy_run_id) REFERENCES mart.strategy_runs(strategy_run_id)
);

do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'mart.backtest_runs'::regclass and conname = 'backtest_runs_succeeded_declares_basis') then alter table mart.backtest_runs add constraint backtest_runs_succeeded_declares_basis CHECK (((status <> 'succeeded'::text) OR ((definition_content_sha256 IS NOT NULL) AND (price_basis IS NOT NULL) AND (claim_ceiling IS NOT NULL) AND (benchmark_rule IS NOT NULL)))) NOT VALID; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'mart.fund_virtual_company'::regclass and conname = 'fund_virtual_company_valuation_basis_check') then alter table mart.fund_virtual_company add constraint fund_virtual_company_valuation_basis_check CHECK ((((valuation_basis = ANY (ARRAY['price_to_sales'::text, 'price_to_gross_profit'::text])) AND (weighted_valuation_gap IS NOT NULL)) OR (valuation_basis IS NULL))) NOT VALID; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'mart.strategy_decisions'::regclass and conname = 'strategy_decisions_one_valuation_basis_check') then alter table mart.strategy_decisions add constraint strategy_decisions_one_valuation_basis_check CHECK ((((target_price_to_sales IS NULL) OR (target_price_to_gross_profit IS NULL)) AND ((current_price_to_gross_profit IS NULL) = (target_price_to_gross_profit IS NULL)) AND ((current_price_to_gross_profit IS NULL) OR (current_price_to_gross_profit > (0)::numeric)))) NOT VALID; end if; end $$;

CREATE OR REPLACE FUNCTION mart.head_freshness(p_refreshed_at timestamp with time zone, p_family text, p_as_of timestamp with time zone DEFAULT now())
 RETURNS TABLE(age_hours numeric, limit_hours integer, freshness text, availability text, staleness_reason text)
 LANGUAGE sql
 STABLE
AS $function$
    with aged as (
        -- A refresh time in the future counts as age 0. A null refresh time has no age.
        select case
                   when p_refreshed_at is null then null
                   else greatest(0, extract(epoch from p_as_of - p_refreshed_at)::numeric / 3600)
               end as hours
    ),
    cap as (
        select (select l.hours from mart.freshness_limit l where l.limit_key = 'withhold') as hours
    ),
    lim as (
        select least(
                   coalesce(
                       (select l.hours from mart.freshness_limit l
                         where l.limit_key = p_family and l.limit_key <> 'withhold'),
                       (select min(l.hours) from mart.freshness_limit l where l.limit_key <> 'withhold')),
                   (select hours from cap)) as hours
    ),
    binding as (
        -- The limit that the age passed. The withhold limit wins when both are passed.
        select case
                   when p_refreshed_at is null then null
                   when aged.hours > cap.hours then cap.hours
                   when aged.hours > lim.hours then lim.hours
               end as hours
        from aged, cap, lim
    )
    select aged.hours as age_hours,
           lim.hours as limit_hours,
           case
               when p_refreshed_at is null then 'unknown'
               when lim.hours is null or aged.hours > lim.hours then 'stale'
               else 'fresh'
           end as freshness,
           case
               when p_refreshed_at is not null and aged.hours <= cap.hours then 'available'
               else 'unavailable'
           end as availability,
           case
               when p_refreshed_at is null then 'refresh_time_unknown'
               when binding.hours is null then null
               when binding.hours % 24 = 0 then 'older_than_' || (binding.hours / 24) || 'd'
               else 'older_than_' || binding.hours || 'h'
           end as staleness_reason
    from aged, cap, lim, binding
$function$;
CREATE OR REPLACE FUNCTION mart.reject_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'current pointer advances are append-only; record a new advance instead';
end;
$function$;
CREATE OR REPLACE FUNCTION mart.require_stored_fetch()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    row_json constant jsonb := to_jsonb(new);
    pointer bigint;
begin
    -- A trigger without its column name, or with a mistyped one, must fail loudly. It would read as NULL
    -- and pass every insert.
    if tg_nargs <> 1 then
        raise exception '% takes exactly one argument, the column of the fetch pointer', tg_name;
    end if;
    if not row_json ? tg_argv[0] then
        raise exception '%.% is not a column of the row', tg_table_name, tg_argv[0];
    end if;
    pointer := (row_json ->> tg_argv[0])::bigint;
    if pointer is not null and not exists (select 1 from raw.fetches where id = pointer) then
        raise exception '%.% % is not a row of raw.fetches', tg_table_name, tg_argv[0], pointer
            using errcode = 'foreign_key_violation';
    end if;
    return new;
end
$function$;

do $$ begin if to_regclass('mart.current_pointer_head') is null then
execute $vc$create or replace view mart.current_pointer_head as
 SELECT DISTINCT ON (current_pointer.environment, current_pointer.universe_id, current_pointer.universe_version, current_pointer.factor_id) current_pointer.pointer_id,
    current_pointer.content_sha256,
    current_pointer.environment,
    current_pointer.universe_id,
    current_pointer.universe_version,
    current_pointer.factor_id,
    current_pointer.target_run_id,
    current_pointer.sequence,
    current_pointer.previous_run_id,
    current_pointer.advanced_at
   FROM mart.current_pointer
  ORDER BY current_pointer.environment, current_pointer.universe_id, current_pointer.universe_version, current_pointer.factor_id, current_pointer.sequence DESC$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.data_engine_identity') is null then
execute $vc$create or replace view mart.data_engine_identity as
 SELECT capture_run_plans.run_id,
    capture_run_plans.release_manifest_id,
    COALESCE(capture_run_plans.payload ->> 'data_engine_git_sha'::text, 'unknown'::text) AS git_sha,
    COALESCE(capture_run_plans.payload ->> 'data_engine_image_digest'::text, 'unknown'::text) AS image_digest,
    capture_run_plans.created_at
   FROM raw.capture_run_plans$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.entity_display_resolution') is null then
execute $vc$create or replace view mart.entity_display_resolution as
 WITH primary_listings AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id AS issuer_id,
            relation.to_entity_id AS listing_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'primary_listing'::text AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), entity_tickers AS (
         SELECT DISTINCT ON (entity_aliases.entity_id) entity_aliases.entity_id,
                CASE
                    WHEN entity_aliases.scheme = 'mic-ticker'::text THEN upper(split_part(entity_aliases.value, ':'::text, 2))
                    WHEN entity_aliases.scheme = 'legacy-id'::text AND entity_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(entity_aliases.value, ':'::text, 3))
                    ELSE NULL::text
                END AS ticker
           FROM staging.entity_aliases
          WHERE entity_aliases.scheme = 'mic-ticker'::text AND NULLIF(btrim(split_part(entity_aliases.value, ':'::text, 2)), ''::text) IS NOT NULL OR entity_aliases.scheme = 'legacy-id'::text AND entity_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
          ORDER BY entity_aliases.entity_id, (entity_aliases.scheme = 'mic-ticker'::text) DESC, entity_aliases.confidence DESC, entity_aliases.transaction_time DESC, entity_aliases.alias_id DESC
        ), latest_members AS (
         SELECT DISTINCT ON (m.issuer_id) m.issuer_id,
            m.listing_id,
                CASE
                    WHEN m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.listing_id::uuid
                    ELSE NULL::uuid
                END AS listing_uuid,
                CASE
                    WHEN m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.issuer_id::uuid
                    ELSE (SELECT ea.entity_id FROM staging.entity_aliases ea WHERE ea.scheme = 'legacy-id'::text AND ea.value = m.issuer_id ORDER BY ea.transaction_time DESC, ea.alias_id DESC LIMIT 1)
                END AS issuer_uuid
           FROM staging.core_snapshot_members m
             LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id =
                CASE
                    WHEN m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.issuer_id::uuid
                    ELSE NULL::uuid
                END AND primary_listing.listing_id =
                CASE
                    WHEN m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.listing_id::uuid
                    ELSE NULL::uuid
                END
          ORDER BY m.issuer_id, (primary_listing.listing_id IS NOT NULL) DESC, m.created_at DESC, m.listing_id
        ), issuer_names AS (
         SELECT DISTINCT ON (entity_aliases.entity_id) entity_aliases.entity_id,
            NULLIF(btrim(entity_aliases.value), ''::text) AS display_name
           FROM staging.entity_aliases
          WHERE entity_aliases.scheme = 'name'::text AND NULLIF(btrim(entity_aliases.value), ''::text) IS NOT NULL
          ORDER BY entity_aliases.entity_id, entity_aliases.confidence DESC, entity_aliases.transaction_time DESC, entity_aliases.alias_id DESC
        ), constituent_names AS (
         SELECT DISTINCT ON ((upper(cf.ticker))) upper(cf.ticker) AS ticker,
            btrim(cf.company_name) AS display_name
           FROM staging.etf_constituent_facts cf
          WHERE NULLIF(btrim(cf.company_name), ''::text) IS NOT NULL AND upper(btrim(cf.company_name)) <> upper(btrim(cf.ticker))
          ORDER BY (upper(cf.ticker)), cf.knowable_at DESC, cf.as_of DESC, cf.id DESC
        ), resolved_tickers AS (
         SELECT lm.issuer_id,
            lm.listing_id,
            lm.issuer_uuid,
            COALESCE(lt.ticker, it.ticker,
                CASE
                    WHEN lm.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(lm.listing_id, ':'::text, 3))
                    ELSE NULL::text
                END) AS ticker
           FROM latest_members lm
             LEFT JOIN entity_tickers lt ON lt.entity_id = lm.listing_uuid
             LEFT JOIN entity_tickers it ON it.entity_id = lm.issuer_uuid
        )
 SELECT rt.issuer_id,
    rt.listing_id,
    rt.ticker,
    COALESCE(
        CASE
            WHEN upper(btrim(inam.display_name)) <> upper(btrim(COALESCE(rt.ticker, ''::text))) THEN NULLIF(btrim(inam.display_name), ''::text)
            ELSE NULL::text
        END, cn.display_name) AS display_name
   FROM resolved_tickers rt
     LEFT JOIN issuer_names inam ON inam.entity_id = rt.issuer_uuid
     LEFT JOIN constituent_names cn ON cn.ticker = upper(rt.ticker)
  ORDER BY rt.issuer_id$vc$;
end if; end $$;
do $$ begin
    if exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart' and c.relname = 'entity_identity' and c.relkind = 'v'
    ) then
        drop view mart.entity_identity;
    end if;
end $$;
do $$ begin if to_regclass('mart.entity_identity') is null then
execute $vc$create materialized view mart.entity_identity as
 WITH RECURSIVE merge_edges AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id,
            relation.to_entity_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'superseded_by'::text AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), survivor_walk(entity_id, current_id, depth) AS (
         SELECT entity.entity_id,
            entity.entity_id,
            0 AS "?column?"
           FROM staging.entities entity
        UNION ALL
         SELECT walk.entity_id,
            edge.to_entity_id,
            walk.depth + 1
           FROM survivor_walk walk
             JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
          WHERE walk.depth < 32
        ), survivors AS (
         SELECT DISTINCT ON (walk.entity_id) walk.entity_id,
            walk.current_id AS id
           FROM survivor_walk walk
          ORDER BY walk.entity_id, walk.depth DESC
        ), alias_retractions AS (
         SELECT retraction.alias_id,
            min(retraction.valid_to) AS valid_to
           FROM staging.entity_retractions retraction
          WHERE retraction.alias_id IS NOT NULL
          GROUP BY retraction.alias_id
        ), active_aliases AS (
         SELECT survivor_1.id AS survivor_id,
            alias.scheme,
            alias.value,
            row_number() OVER (PARTITION BY survivor_1.id, alias.scheme ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC) AS rn
           FROM staging.entity_aliases alias
             JOIN survivors survivor_1 ON survivor_1.entity_id = alias.entity_id
             LEFT JOIN alias_retractions retracted ON retracted.alias_id = alias.alias_id
          WHERE LEAST(alias.valid_to, retracted.valid_to) IS NULL OR LEAST(alias.valid_to, retracted.valid_to) > alias.valid_from
        ), pivoted_aliases AS MATERIALIZED (
         SELECT active_aliases.survivor_id,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'cik'::text) AS cik,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'lei'::text) AS lei,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'name'::text) AS name,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'legacy-id'::text) AS legacy_id,
            COALESCE(
                max(split_part(active_aliases.value, ':'::text, 2)) FILTER (WHERE active_aliases.scheme = 'mic-ticker'::text),
                max(upper(split_part(active_aliases.value, ':'::text, 3))) FILTER (
                    WHERE active_aliases.scheme = 'legacy-id'::text
                      AND active_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                ),
                max(upper(active_aliases.value)) FILTER (WHERE active_aliases.scheme = 'ticker'::text)
            ) AS direct_ticker,
            CASE
                WHEN bool_or(active_aliases.scheme = 'legacy-id'::text) THEN max(
                    CASE
                        WHEN active_aliases.scheme = 'legacy-id'::text
                         AND active_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                        THEN active_aliases.value
                    END
                )
                ELSE max(
                    CASE
                        WHEN active_aliases.scheme = 'mic-ticker'::text
                        THEN (('listing:'::text || lower(split_part(active_aliases.value, ':'::text, 1))) || ':'::text) || lower(split_part(active_aliases.value, ':'::text, 2))
                    END
                )
            END AS direct_listing
           FROM active_aliases
          WHERE active_aliases.rn = 1
          GROUP BY active_aliases.survivor_id
        ), primary_listings AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id AS issuer_id,
            relation.to_entity_id AS listing_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'primary_listing'::text AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), issuer_rels AS MATERIALIZED (
         SELECT r_issues.from_entity_id AS issuer_id,
            (primary_listing.listing_id IS NOT NULL) AS is_primary,
            r_issues.transaction_time,
            r_issues.relation_id,
            pa.direct_ticker,
            pa.direct_listing
           FROM staging.entity_relations r_issues
             JOIN staging.entity_relations r_listed ON r_listed.from_entity_id = r_issues.to_entity_id AND r_listed.relation_type = 'listed_as'::text
             JOIN pivoted_aliases pa ON pa.survivor_id = r_listed.to_entity_id
             LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id = r_issues.from_entity_id AND primary_listing.listing_id = r_listed.to_entity_id
          WHERE r_issues.relation_type = 'issues'::text
        ), issuer_tickers AS (
         SELECT DISTINCT ON (issuer_rels.issuer_id) issuer_rels.issuer_id,
            issuer_rels.direct_ticker AS ticker
           FROM issuer_rels
          WHERE issuer_rels.direct_ticker IS NOT NULL
          ORDER BY issuer_rels.issuer_id, issuer_rels.is_primary DESC, issuer_rels.transaction_time DESC, issuer_rels.direct_ticker, issuer_rels.relation_id DESC
        ), instrument_tickers AS (
         SELECT DISTINCT ON (r_listed.from_entity_id) r_listed.from_entity_id AS instrument_id,
            pa.direct_ticker AS ticker
           FROM staging.entity_relations r_listed
             JOIN pivoted_aliases pa ON pa.survivor_id = r_listed.to_entity_id
          WHERE r_listed.relation_type = 'listed_as'::text AND pa.direct_ticker IS NOT NULL
          ORDER BY r_listed.from_entity_id, r_listed.transaction_time DESC, pa.direct_ticker, r_listed.relation_id DESC
        ), issuer_listings AS (
         SELECT DISTINCT ON (issuer_rels.issuer_id) issuer_rels.issuer_id,
            issuer_rels.direct_listing AS listing_id
           FROM issuer_rels
          WHERE issuer_rels.direct_listing IS NOT NULL
          ORDER BY issuer_rels.issuer_id, issuer_rels.is_primary DESC, issuer_rels.transaction_time DESC, issuer_rels.direct_listing, issuer_rels.relation_id DESC
        )
 SELECT e.entity_id,
    e.kind,
    COALESCE(pa.direct_ticker, it.ticker, inst.ticker) AS current_ticker,
    CASE
        WHEN upper(btrim(pa.name)) <> upper(btrim(COALESCE(pa.direct_ticker, it.ticker, inst.ticker, ''::text))) THEN NULLIF(btrim(pa.name), ''::text)
        ELSE NULL::text
    END AS name,
    pa.cik,
    pa.lei,
    COALESCE(pa.direct_listing, ilist.listing_id) AS listing_id,
    pa.legacy_id
   FROM staging.entities e
     JOIN survivors survivor ON survivor.entity_id = e.entity_id
     LEFT JOIN pivoted_aliases pa ON pa.survivor_id = survivor.id
     LEFT JOIN issuer_tickers it ON it.issuer_id = survivor.id
     LEFT JOIN instrument_tickers inst ON inst.instrument_id = survivor.id
     LEFT JOIN issuer_listings ilist ON ilist.issuer_id = survivor.id
WITH DATA$vc$;
end if; end $$;
do $$
declare
    v_rel text;
    v_kind "char";
begin
    if exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart'
          and c.relname in ('fund_holdings', 'fund_holdings_resolved', 'fund_holdings_coverage', 'fund_holdings_valuation')
          and c.relkind = 'v'
    ) then
        for v_rel in
            select unnest(array['fund_holdings_valuation', 'fund_holdings_coverage', 'fund_holdings_resolved', 'fund_holdings'])
        loop
            select c.relkind into v_kind
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'mart' and c.relname = v_rel;
            if v_kind = 'm' then
                execute format('drop materialized view mart.%I', v_rel);
            elsif v_kind = 'v' then
                execute format('drop view mart.%I', v_rel);
            end if;
        end loop;
    end if;
end $$;
do $$ begin if to_regclass('mart.fund_holdings') is null then
execute $vc$create materialized view mart.fund_holdings as
 SELECT DISTINCT ON (facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name) facts.fund_id::text AS fund_id,
    fund_name.value AS fund_name,
    facts.holding_id::text AS holding_id,
    facts.holding_name,
    facts.isin,
    facts.cusip,
    facts.report_period,
    facts.transaction_time,
    facts.percent_of_net_assets,
    facts.value_usd,
    facts.balance,
    facts.confidence,
    facts.raw_ref
   FROM staging.fund_holding_facts facts
     LEFT JOIN LATERAL ( SELECT alias.value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = facts.fund_id AND alias.scheme = 'name'::text
          ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC
         LIMIT 1) fund_name ON true
  ORDER BY facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name, facts.recorded_at DESC, facts.id DESC
WITH DATA$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.macro_series_current') is null then
execute $vc$create or replace view mart.macro_series_current as
 SELECT DISTINCT ON (macro_series_daily.series_id, macro_series_daily.observation_date) macro_series_daily.series_id,
    macro_series_daily.observation_date,
    macro_series_daily.value,
    macro_series_daily.knowable_at,
    macro_series_daily.source,
    macro_series_daily.fetch_id,
    macro_series_daily.fetched_at,
    macro_series_daily.confidence,
    macro_series_daily.recorded_at
   FROM mart.macro_series_daily
  ORDER BY macro_series_daily.series_id, macro_series_daily.observation_date DESC, macro_series_daily.recorded_at DESC, macro_series_daily.id DESC$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.capture_meta_info') is null then
execute $vc$create or replace view mart.capture_meta_info as
 SELECT obligation.run_id,
    obligation.obligation_id,
    result.logical_obligation_id,
    obligation.subject_kind,
    obligation.subject_id,
    obligation.capture_requirement_id,
    obligation.partition_key,
    binding.work_item_id,
    work.source_request_id,
    request.source_registry_entry_id,
    request.source_policy_id,
    request.request_fingerprint_version,
    result.terminal_state,
    result.reason_codes,
    result.completed_at,
    COALESCE(attempts.attempt_count, 0::bigint)::integer AS attempt_count,
    final_attempt_result.status_code AS final_status_code,
    observation.observation_id,
    observation.semantic_version,
    observation.parser_version,
    observation.mapping_version,
    observation.confidence,
        CASE
            WHEN observation.observation_id IS NULL THEN NULL::text
            WHEN (campaign.cutoff - observation.knowable_at) <= COALESCE(NULLIF(policy.semantic_freshness_max_age ->> observation.semantic_type, ''::text)::interval, policy.freshness_max_age) THEN 'fresh'::text
            ELSE 'stale'::text
        END AS freshness_state,
    observation.knowable_at,
    observation.recorded_at
   FROM raw.capture_obligations obligation
     JOIN raw.capture_campaigns campaign USING (campaign_id)
     LEFT JOIN raw.capture_obligation_work_bindings binding ON binding.obligation_id = obligation.obligation_id
     LEFT JOIN raw.capture_work_items work USING (work_item_id)
     LEFT JOIN raw.capture_schedule_policies policy USING (schedule_policy_id)
     LEFT JOIN raw.capture_source_requests request USING (source_request_id)
     LEFT JOIN raw.capture_obligation_results result ON result.capture_obligation_id = obligation.obligation_id
     LEFT JOIN raw.capture_attempt_results final_attempt_result ON final_attempt_result.attempt_id = result.final_attempt_id
     LEFT JOIN LATERAL ( SELECT count(*) AS attempt_count
           FROM raw.capture_attempts attempt
          WHERE attempt.work_item_id = work.work_item_id) attempts ON true
     LEFT JOIN LATERAL ( SELECT selected.observation_id,
            selected.content_sha256,
            selected.capture_obligation_id,
            selected.source_vintage_id,
            selected.semantic_type,
            selected.semantic_version,
            selected.subject_kind,
            selected.subject_id,
            selected.valid_from,
            selected.valid_to,
            selected.knowable_at,
            selected.parser_version,
            selected.mapping_version,
            selected.normalized_payload_sha256,
            selected.is_restatement,
            selected.supersedes_observation_id,
            selected.confidence,
            selected.freshness_state,
            selected.payload,
            selected.recorded_at,
            selected.selection_count
           FROM ( SELECT candidate.observation_id,
                    candidate.content_sha256,
                    candidate.capture_obligation_id,
                    candidate.source_vintage_id,
                    candidate.semantic_type,
                    candidate.semantic_version,
                    candidate.subject_kind,
                    candidate.subject_id,
                    candidate.valid_from,
                    candidate.valid_to,
                    candidate.knowable_at,
                    candidate.parser_version,
                    candidate.mapping_version,
                    candidate.normalized_payload_sha256,
                    candidate.is_restatement,
                    candidate.supersedes_observation_id,
                    candidate.confidence,
                    candidate.freshness_state,
                    candidate.payload,
                    candidate.recorded_at,
                    count(*) OVER () AS selection_count
                   FROM staging.capture_observation_obligations usage
                     JOIN staging.capture_normalized_observations candidate USING (observation_id)
                     JOIN raw.capture_source_vintages vintage ON vintage.source_vintage_id = candidate.source_vintage_id AND vintage.source_request_id = work.source_request_id
                  WHERE usage.capture_obligation_id = obligation.obligation_id AND candidate.source_vintage_id = COALESCE(final_attempt_result.source_vintage_id, final_attempt_result.reused_source_vintage_id) AND candidate.subject_kind = obligation.subject_kind AND candidate.subject_id = obligation.subject_id AND candidate.semantic_type = regexp_replace(obligation.capture_requirement_id, ':v1$'::text, ''::text) AND (candidate.valid_from AT TIME ZONE 'UTC'::text)::date <= (campaign.cutoff AT TIME ZONE 'UTC'::text)::date AND (candidate.valid_to IS NULL OR (candidate.valid_to AT TIME ZONE 'UTC'::text)::date >= (campaign.cutoff AT TIME ZONE 'UTC'::text)::date) AND candidate.knowable_at <= campaign.cutoff) selected
          WHERE selected.selection_count = 1) observation ON true$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.capture_status') is null then
execute $vc$create or replace view mart.capture_status as
 SELECT run.run_id,
    run.campaign_id,
    campaign.environment,
    campaign.cutoff,
    list_version.universe_id,
    list_version.universe_version,
    list_version.universe_sha256,
    count(obligation.obligation_id)::integer AS obligation_count,
    count(result.result_id)::integer AS terminal_count,
    count(*) FILTER (WHERE result.terminal_state = 'success'::text)::integer AS success_count,
    count(*) FILTER (WHERE result.terminal_state = 'unchanged'::text)::integer AS unchanged_count,
    count(*) FILTER (WHERE result.terminal_state = 'unavailable'::text)::integer AS unavailable_count,
    count(*) FILTER (WHERE result.terminal_state = 'skipped_by_policy'::text)::integer AS skipped_count,
    count(*) FILTER (WHERE result.terminal_state = 'failed'::text)::integer AS failed_count,
    count(result.result_id) = count(obligation.obligation_id) AS complete
   FROM raw.capture_runs run
     JOIN raw.capture_campaigns campaign USING (campaign_id)
     JOIN raw.capture_obligations obligation USING (run_id)
     JOIN raw.capture_list_versions list_version USING (list_version_id)
     LEFT JOIN raw.capture_obligation_results result ON result.capture_obligation_id = obligation.obligation_id
  GROUP BY run.run_id, run.campaign_id, campaign.environment, campaign.cutoff, list_version.universe_id, list_version.universe_version, list_version.universe_sha256$vc$;
end if; end $$;
do $$ begin if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'mart' and c.relname = 'capture_issuer_lineage' and md5(pg_get_viewdef(c.oid)) = '059da77fe78ca5c7d1b9ce526f8b5029') then
execute $vc$create or replace view mart.capture_issuer_lineage as
 SELECT ('core-result:'::text || m.member_sha256) AS result_id,
    s.snapshot_id,
    s.run_id,
    s.release_manifest_id,
    s.universe_id,
    s.universe_version,
    s.universe_sha256,
    s.cutoff,
    m.issuer_id,
    m.instrument_id,
    m.listing_id,
    COALESCE(m.factor_input ->> 'operating_branch'::text, 'non_financial'::text) AS operating_branch,
    m.observation_ids AS input_observation_ids,
    mnv.tree_id,
    mnv.tree_version,
    mnv.tree_sha256 AS gppe_definition_sha256,
    mnv.confidence,
    periods.operating_period_end,
    periods.revenue_period_end,
    periods.shares_period_end,
    s.created_at,
    ( SELECT jsonb_agg(item.entry ORDER BY item.observation_id) AS items
           FROM ( SELECT DISTINCT ON (observation.observation_id) observation.observation_id,
                    jsonb_build_object('observation_id', observation.observation_id, 'semantic_type', observation.semantic_type, 'semantic_version', observation.semantic_version, 'source_vintage_id', observation.source_vintage_id, 'source_request_id', vintage.source_request_id, 'source_registry_entry_id', request.source_registry_entry_id, 'source_policy_id', request.source_policy_id, 'parser_version', observation.parser_version, 'mapping_version', observation.mapping_version, 'normalized_payload_sha256', observation.normalized_payload_sha256, 'confidence', observation.confidence, 'freshness',
                        CASE
                            WHEN (s.cutoff - observation.knowable_at) <= COALESCE(NULLIF(policy.semantic_freshness_max_age ->> observation.semantic_type, ''::text)::interval, policy.freshness_max_age) THEN 'fresh'::text
                            ELSE 'stale'::text
                        END, 'knowable_at', observation.knowable_at, 'recorded_at', observation.recorded_at, 'vintage', payload.normalized_payload -> 'vintage'::text, 'inputs', ( SELECT COALESCE(jsonb_object_agg(field.key, field.value), '{}'::jsonb) AS "coalesce"
                           FROM jsonb_each(payload.normalized_payload) field(key, value)
                          WHERE field.key = ANY (ARRAY['gross_profit'::text, 'total_assets'::text, 'headcount'::text, 'revenue'::text, 'shares_outstanding'::text, 'close'::text, 'currency'::text, 'listing_id'::text]))) AS entry
                   FROM unnest(m.observation_ids) selected(observation_id)
                     JOIN staging.capture_normalized_observations observation USING (observation_id)
                     JOIN staging.capture_observation_obligations usage USING (observation_id)
                     JOIN staging.capture_observation_payloads payload USING (observation_id)
                     JOIN raw.capture_obligations obligation ON obligation.obligation_id = usage.capture_obligation_id AND obligation.run_id = s.run_id
                     JOIN raw.capture_obligation_work_bindings binding ON binding.obligation_id = obligation.obligation_id
                     JOIN raw.capture_work_items work USING (work_item_id)
                     JOIN raw.capture_schedule_policies policy USING (schedule_policy_id)
                     JOIN raw.capture_source_vintages vintage USING (source_vintage_id)
                     JOIN raw.capture_source_requests request ON request.source_request_id = vintage.source_request_id
                  ORDER BY observation.observation_id, obligation.obligation_id) item) AS lineage
   FROM staging.core_snapshots s
     JOIN staging.core_snapshot_members m USING (snapshot_id)
     LEFT JOIN mart.metric_node_values mnv ON mnv.run_id = s.run_id AND mnv.issuer_id = m.issuer_id AND mnv.node_key = CASE WHEN COALESCE(m.factor_input ->> 'operating_branch'::text, 'non_financial'::text) = 'financial'::text THEN 'gppe_banking_tce_v1'::text ELSE 'gppe_uniform_charge_v0'::text END
     LEFT JOIN LATERAL ( SELECT (NULLIF(p.normalized_payload ->> 'operating_period_end'::text, ''::text))::date AS operating_period_end,
            (NULLIF(p.normalized_payload ->> 'revenue_period_end'::text, ''::text))::date AS revenue_period_end,
            (NULLIF(p.normalized_payload ->> 'shares_period_end'::text, ''::text))::date AS shares_period_end
           FROM unnest(m.observation_ids) sel(observation_id)
             JOIN staging.capture_normalized_observations o USING (observation_id)
             JOIN staging.capture_observation_payloads p USING (observation_id)
          WHERE o.semantic_type = 'financial-fact'::text
          ORDER BY o.knowable_at DESC, o.observation_id DESC
         LIMIT 1) periods ON true$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.governed_strategy_run') is null then
execute $vc$create or replace view mart.governed_strategy_run as
    with head as (
        select distinct on (regexp_replace(current_pointer_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''))
            current_pointer_head.environment,
            current_pointer_head.universe_id,
            current_pointer_head.universe_version,
            current_pointer_head.factor_id,
            current_pointer_head.target_run_id,
            current_pointer_head.sequence,
            current_pointer_head.advanced_at
        from mart.current_pointer_head
        where current_pointer_head.environment = ((
            select environment_identity.environment
            from mart.environment_identity
        ))
          and current_pointer_head.factor_id = 'gross_profit_per_employee'::text
          and current_pointer_head.universe_id not like 'universe:canary-%'::text
        order by regexp_replace(current_pointer_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''),
                 current_pointer_head.advanced_at desc
    )
    select head.environment,
        head.universe_id,
        head.universe_version,
        head.factor_id,
        head.target_run_id,
        head.sequence,
        head.advanced_at,
        campaign.cutoff,
        run.strategy_run_id,
        run.strategy_key,
        run.executed_at
    from head
    join raw.capture_runs cr on cr.run_id = head.target_run_id
    join raw.capture_campaigns campaign using (campaign_id)
    join lateral (
        select distinct on (candidate.strategy_key)
            candidate.strategy_run_id,
            candidate.strategy_key,
            candidate.executed_at
        from mart.strategy_run_capture_bindings binding
        join mart.strategy_runs candidate on candidate.strategy_run_id = binding.strategy_run_id
        where binding.capture_run_id = head.target_run_id
        order by candidate.strategy_key, binding.bound_at desc, candidate.strategy_run_id desc
    ) run on true$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.served_head') is null then
execute $vc$create or replace view mart.served_head as
 SELECT h.environment,
    h.universe_id,
    h.universe_version,
    h.factor_id,
    h.sequence,
    h.advanced_at,
    h.target_run_id AS head_run_id,
        CASE
            WHEN f.availability = 'unavailable'::text THEN NULL::text
            ELSE h.target_run_id
        END AS run_id,
    a.artifact_key,
    a.family,
    f.age_hours,
    f.limit_hours,
    f.freshness,
    f.availability,
    f.staleness_reason
   FROM ( SELECT p.environment,
            p.universe_id,
            p.universe_version,
            p.factor_id,
            p.sequence,
            p.advanced_at,
            p.target_run_id,
            row_number() OVER (PARTITION BY p.environment, (regexp_replace(p.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text, ''::text)), p.factor_id ORDER BY p.advanced_at DESC, p.universe_id DESC, p.universe_version DESC) AS version_rank
           FROM mart.current_pointer_head p) h
     LEFT JOIN LATERAL ( SELECT s.artifact_key,
            s.family
           FROM mart.served_artifact s
             JOIN mart.freshness_limit l ON l.limit_key = s.family
          WHERE s.universe_like IS NOT NULL AND h.universe_id ~~ s.universe_like
          ORDER BY l.hours, s.artifact_key
         LIMIT 1) a ON true
     CROSS JOIN LATERAL mart.head_freshness(h.advanced_at, a.family) f(age_hours, limit_hours, freshness, availability, staleness_reason)
  WHERE h.environment = (( SELECT environment_identity.environment
           FROM mart.environment_identity)) AND h.version_rank = 1$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.served_head_environments') is null then
execute $vc$create or replace view mart.served_head_environments as
 SELECT h.environment,
    count(*) AS heads,
    NOT h.environment IS DISTINCT FROM (( SELECT environment_identity.environment
           FROM mart.environment_identity)) AS served
   FROM mart.current_pointer_head h
  GROUP BY h.environment$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.fund_holdings_resolved') is null then
execute $vc$create materialized view mart.fund_holdings_resolved as
 WITH RECURSIVE merge_edges AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id,
            relation.to_entity_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'superseded_by'::text
            AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), survivor_walk(entity_id, current_id, depth) AS (
         SELECT entity.entity_id,
            entity.entity_id,
            0 AS depth
           FROM staging.entities entity
        UNION ALL
         SELECT walk.entity_id,
            edge.to_entity_id,
            walk.depth + 1
           FROM survivor_walk walk
             JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
          WHERE walk.depth < 32
        ), survivors AS (
         SELECT DISTINCT ON (walk.entity_id) walk.entity_id,
            walk.current_id AS survivor_id
           FROM survivor_walk walk
          ORDER BY walk.entity_id, walk.depth DESC
        ), resolved AS (
         SELECT holdings.fund_id,
            holdings.fund_name,
            holdings.holding_id,
            holdings.holding_name,
            holdings.isin,
            holdings.cusip,
            holdings.report_period,
            holdings.transaction_time,
            holdings.percent_of_net_assets,
            holdings.value_usd,
            holdings.balance,
            holdings.confidence,
            holdings.raw_ref,
            COALESCE(
                NULLIF(holdings.holding_id, ''::text)::uuid,
                ( SELECT alias.entity_id
                       FROM staging.entity_aliases alias
                      WHERE alias.scheme = 'isin'::text AND alias.value = upper(holdings.isin)
                      ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
                     LIMIT 1)
            ) AS holding_uuid
           FROM mart.fund_holdings holdings
        ), linked AS (
         SELECT resolved.*,
            COALESCE(
                ( SELECT COALESCE(sup.survivor_id, rel.from_entity_id)
                       FROM staging.entity_relations rel
                         LEFT JOIN survivors sup ON sup.entity_id = rel.from_entity_id
                      WHERE rel.to_entity_id = resolved.holding_uuid AND rel.relation_type = 'issues'::text
                      ORDER BY rel.transaction_time DESC, rel.relation_id DESC
                     LIMIT 1),
                COALESCE(self_sup.survivor_id, resolved.holding_uuid)
            ) AS issuer_uuid
           FROM resolved
             LEFT JOIN survivors self_sup ON self_sup.entity_id = resolved.holding_uuid
        )
 SELECT linked.fund_id,
    linked.fund_name,
    linked.report_period,
    linked.transaction_time,
    linked.holding_name,
    linked.isin,
    linked.cusip,
    linked.percent_of_net_assets,
    linked.value_usd,
    linked.issuer_uuid::text AS issuer_entity,
    COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) AS ticker,
    COALESCE(
        listing_alias.listing_id,
        CASE
            WHEN COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) IS NOT NULL THEN 'listing:xnas:'::text || lower(COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value))
            ELSE NULL::text
        END
    ) AS listing_id
   FROM linked
     LEFT JOIN LATERAL ( SELECT
                CASE
                    WHEN alias.scheme = 'mic-ticker'::text THEN upper(split_part(alias.value, ':'::text, 2))
                    WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(alias.value, ':'::text, 3))
                    WHEN alias.scheme = 'ticker'::text THEN upper(alias.value)
                    ELSE NULL::text
                END AS ticker,
                CASE
                    WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN alias.value
                    WHEN alias.scheme = 'mic-ticker'::text THEN ('listing:'::text || lower(split_part(alias.value, ':'::text, 1))) || ':'::text || lower(split_part(alias.value, ':'::text, 2))
                    ELSE NULL::text
                END AS listing_id
           FROM staging.entity_relations rel
             JOIN staging.entity_aliases alias ON alias.entity_id = rel.to_entity_id
          WHERE rel.from_entity_id = linked.holding_uuid
            AND rel.relation_type = 'listed_as'::text
            AND (alias.scheme = 'mic-ticker'::text OR alias.scheme = 'ticker'::text OR (alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text))
          ORDER BY (alias.scheme = 'mic-ticker'::text) DESC, (alias.scheme = 'legacy-id'::text) DESC, alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC
         LIMIT 1) listing_alias ON true
     LEFT JOIN LATERAL ( SELECT upper(alias.value) AS value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = linked.holding_uuid AND alias.scheme = 'ticker'::text
          ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
         LIMIT 1) holding_ticker ON true
     LEFT JOIN LATERAL ( SELECT upper(alias.value) AS value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = linked.issuer_uuid AND alias.scheme = 'ticker'::text
          ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
         LIMIT 1) issuer_ticker ON true
WITH DATA$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.macro_series_status') is null then
execute $vc$create or replace view mart.macro_series_status as
 WITH newest AS (
         SELECT DISTINCT ON (macro_series_current.series_id) macro_series_current.series_id,
            macro_series_current.observation_date,
            macro_series_current.value,
            macro_series_current.source,
            macro_series_current.fetched_at
           FROM mart.macro_series_current
          ORDER BY macro_series_current.series_id, macro_series_current.observation_date DESC
        )
 SELECT s.series_id,
    s.label,
    s.unit,
    s.source_id,
    s.max_age_days,
    n.observation_date,
    n.value,
    n.source,
    n.fetched_at,
    (now() AT TIME ZONE 'UTC'::text)::date - n.observation_date AS age_days,
    COALESCE(((now() AT TIME ZONE 'UTC'::text)::date - n.observation_date) <= s.max_age_days, false) AS is_fresh
   FROM mart.macro_series s
     LEFT JOIN newest n USING (series_id)$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.strategy_run_capture') is null then
execute $vc$create or replace view mart.strategy_run_capture as
 SELECT DISTINCT ON (binding.strategy_run_id) binding.strategy_run_id,
    binding.capture_run_id,
    governed.strategy_run_id IS NOT NULL AS governed
   FROM mart.strategy_run_capture_bindings binding
     LEFT JOIN mart.governed_strategy_run governed ON governed.strategy_run_id = binding.strategy_run_id AND governed.target_run_id = binding.capture_run_id
  ORDER BY binding.strategy_run_id, (governed.strategy_run_id IS NOT NULL) DESC, binding.bound_at DESC, binding.capture_run_id DESC$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.fund_holdings_coverage') is null then
execute $vc$create materialized view mart.fund_holdings_coverage as
 SELECT fund_holdings_resolved.fund_id,
    fund_holdings_resolved.report_period,
    fund_holdings_resolved.transaction_time,
    count(*) AS lines,
    count(*) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL) AS resolved_lines,
    COALESCE(sum(fund_holdings_resolved.percent_of_net_assets), 0::numeric) AS total_weight_pct,
    COALESCE(sum(fund_holdings_resolved.percent_of_net_assets) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL), 0::numeric) AS resolved_weight_pct
   FROM mart.fund_holdings_resolved
  GROUP BY fund_holdings_resolved.fund_id, fund_holdings_resolved.report_period, fund_holdings_resolved.transaction_time
WITH DATA$vc$;
end if; end $$;
do $$ begin if to_regclass('mart.fund_holdings_valuation') is null then
execute $vc$create materialized view mart.fund_holdings_valuation as
 WITH newest AS (
         SELECT DISTINCT ON (fund_holdings.fund_id) fund_holdings.fund_id,
            fund_holdings.report_period,
            fund_holdings.transaction_time
           FROM mart.fund_holdings
          ORDER BY fund_holdings.fund_id, fund_holdings.report_period DESC, fund_holdings.transaction_time DESC
        )
 SELECT resolved.fund_id,
    resolved.fund_name,
    resolved.report_period,
    resolved.transaction_time,
    resolved.holding_name,
    resolved.isin,
    resolved.cusip,
    resolved.percent_of_net_assets,
    resolved.value_usd,
    resolved.issuer_entity,
    resolved.ticker,
    resolved.listing_id
   FROM mart.fund_holdings_resolved resolved
     JOIN newest USING (fund_id, report_period, transaction_time)
WITH DATA$vc$;
end if; end $$;

CREATE OR REPLACE FUNCTION staging.validate_core_snapshot()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    capture_status mart.capture_status%rowtype;
    release_exists boolean;
    release_matches_plan boolean;
begin
    select * into capture_status from mart.capture_status where run_id = new.run_id;
    if capture_status.run_id is null
       or capture_status.environment <> (select environment from mart.environment_identity)
       or capture_status.obligation_count <> new.observation_count
       or capture_status.terminal_count <> capture_status.obligation_count
       or capture_status.success_count + capture_status.unchanged_count <> capture_status.obligation_count
       or capture_status.unavailable_count <> 0
       or capture_status.skipped_count <> 0
       or capture_status.failed_count <> 0
       or not capture_status.complete
       or capture_status.universe_id <> new.universe_id
       or capture_status.universe_version <> new.universe_version
       or capture_status.universe_sha256 <> new.universe_sha256
       or capture_status.cutoff <> new.cutoff then
        raise check_violation using message = 'core snapshot requires one complete exact capture run matching its own obligation count';
    end if;
    select exists (
        select 1 from staging.contract_objects
         where contract_id = new.release_manifest_id and contract_kind = 'release_manifest'
    ) into release_exists;
    select exists (
        select 1 from raw.capture_run_plans
         where run_id = new.run_id and release_manifest_id = new.release_manifest_id
    ) into release_matches_plan;
    if not release_exists or not release_matches_plan then
        raise check_violation using message = 'core snapshot release is not durable or does not match its run plan';
    end if;
    if raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'core snapshot payload hash does not match';
    end if;
    return new;
end;
$function$;

do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.cell_confidence'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '039fcc02027ec0ce5f28254de297fac9') then drop trigger if exists trg_mart_cell_confidence_immutable on mart.cell_confidence; CREATE TRIGGER trg_mart_cell_confidence_immutable BEFORE DELETE OR UPDATE ON mart.cell_confidence FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.current_pointer'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'acfea4a797cbf4aed70baf0aca1efe6c') then drop trigger if exists reject_mutation on mart.current_pointer; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.current_pointer FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.datahub_confidence_report'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'c2142e8cce2cca89299ebd9541dd23f1') then drop trigger if exists reject_mutation on mart.datahub_confidence_report; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.datahub_confidence_report FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.datahub_quality_report'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'a1cfb664c102fdea7af460cd59b5126e') then drop trigger if exists reject_mutation on mart.datahub_quality_report; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.datahub_quality_report FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.macro_series_daily'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'e42217e1f50eb68e9d618ed3c0acc131') then drop trigger if exists reject_mutation on mart.macro_series_daily; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.macro_series_daily FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.macro_series_daily'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '80ea72b8d54b6e59b77365f89addbc47') then drop trigger if exists require_stored_fetch on mart.macro_series_daily; CREATE TRIGGER require_stored_fetch BEFORE INSERT ON mart.macro_series_daily FOR EACH ROW EXECUTE FUNCTION mart.require_stored_fetch('fetch_id'); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.metric_node_values'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '87be4c5c674fd7caced276389918dade') then drop trigger if exists reject_mutation on mart.metric_node_values; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.metric_node_values FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.nightly_verdicts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '91f17ade1c36c49bd44a8723a9c9f0a7') then drop trigger if exists reject_mutation on mart.nightly_verdicts; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.nightly_verdicts FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.oracle_outcomes'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'a1de3f75d3d027f7da5c6e9aca69587e') then drop trigger if exists reject_mutation on mart.oracle_outcomes; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.oracle_outcomes FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.research_card_artifacts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'e6ccf7c0b2df791fad40662767958804') then drop trigger if exists trg_mart_research_card_artifacts_immutable on mart.research_card_artifacts; CREATE TRIGGER trg_mart_research_card_artifacts_immutable BEFORE DELETE OR UPDATE ON mart.research_card_artifacts FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.strategy_decisions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '6ced65234b03ac485076628b5baa0008') then drop trigger if exists trg_strategy_decisions_append_only on mart.strategy_decisions; CREATE TRIGGER trg_strategy_decisions_append_only BEFORE DELETE OR UPDATE ON mart.strategy_decisions FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.strategy_input_coverage'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '98ecb5f99989100dba04a66d60c2b9ee') then drop trigger if exists reject_mutation on mart.strategy_input_coverage; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.strategy_input_coverage FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.strategy_run_capture_bindings'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '0b9cdaf2ffa58ea702e3cb6685d4ba43') then drop trigger if exists trg_strategy_run_capture_bindings_append_only on mart.strategy_run_capture_bindings; CREATE TRIGGER trg_strategy_run_capture_bindings_append_only BEFORE DELETE OR UPDATE ON mart.strategy_run_capture_bindings FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.strategy_runs'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '955638e2a1989bd4f24a4ff64a3b690a') then drop trigger if exists require_stored_fetch on mart.strategy_runs; CREATE TRIGGER require_stored_fetch BEFORE INSERT ON mart.strategy_runs FOR EACH ROW EXECUTE FUNCTION mart.require_stored_fetch('risk_free_fetch_id'); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'mart.strategy_runs'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '12e3dbdeafd06c9d14e5130eee178e38') then drop trigger if exists trg_strategy_runs_append_only on mart.strategy_runs; CREATE TRIGGER trg_strategy_runs_append_only BEFORE DELETE OR UPDATE ON mart.strategy_runs FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation(); end if; end $$;

do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.core_snapshots'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '8b33b1bf393eb684b9370d3f0927bfd4') then drop trigger if exists validate_snapshot on staging.core_snapshots; CREATE TRIGGER validate_snapshot BEFORE INSERT ON staging.core_snapshots FOR EACH ROW EXECUTE FUNCTION staging.validate_core_snapshot(); end if; end $$;

do $$ begin if to_regclass('mart.idx_backtest_runs_strategy') is null then CREATE INDEX idx_backtest_runs_strategy ON mart.backtest_runs USING btree (strategy_key, executed_at DESC); end if; end $$;
do $$ begin if to_regclass('mart.idx_backtest_trades_run') is null then CREATE INDEX idx_backtest_trades_run ON mart.backtest_trades USING btree (run_id, trade_date); end if; end $$;
do $$ begin if to_regclass('mart.idx_mart_cell_confidence_observation') is null then CREATE INDEX idx_mart_cell_confidence_observation ON mart.cell_confidence USING btree (observation_id) WHERE (observation_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('mart.ix_datahub_confidence_report_universe') is null then CREATE INDEX ix_datahub_confidence_report_universe ON mart.datahub_confidence_report USING btree (universe_id, created_at DESC); end if; end $$;
do $$ begin if to_regclass('mart.ix_fund_virtual_company_fund') is null then CREATE INDEX ix_fund_virtual_company_fund ON mart.fund_virtual_company USING btree (fund_id, cutoff DESC); end if; end $$;
do $$ begin if to_regclass('mart.ix_issuer_analyst_ratings_cutoff') is null then CREATE INDEX ix_issuer_analyst_ratings_cutoff ON mart.issuer_analyst_ratings USING btree (cutoff DESC, consensus_rating DESC NULLS LAST); end if; end $$;
do $$ begin if to_regclass('mart.ix_issuer_supply_chain_exposure_cutoff') is null then CREATE INDEX ix_issuer_supply_chain_exposure_cutoff ON mart.issuer_supply_chain_exposure USING btree (cutoff DESC, exposure_score DESC NULLS LAST); end if; end $$;
do $$ begin if to_regclass('mart.ix_issuer_theme_purity_ranking') is null then CREATE INDEX ix_issuer_theme_purity_ranking ON mart.issuer_theme_purity USING btree (theme_id, cutoff DESC, theme_share DESC NULLS LAST); end if; end $$;
do $$ begin if to_regclass('mart.ix_macro_series_daily_asof') is null then CREATE INDEX ix_macro_series_daily_asof ON mart.macro_series_daily USING btree (series_id, observation_date DESC, recorded_at DESC, id DESC); end if; end $$;
do $$ begin if to_regclass('mart.ix_metric_node_values_issuer_key') is null then CREATE INDEX ix_metric_node_values_issuer_key ON mart.metric_node_values USING btree (issuer_id, node_key); end if; end $$;
do $$ begin if to_regclass('mart.ix_metric_node_values_run_key') is null then CREATE INDEX ix_metric_node_values_run_key ON mart.metric_node_values USING btree (run_id, node_key); end if; end $$;
do $$ begin if to_regclass('mart.ix_nightly_verdicts_latest') is null then CREATE INDEX ix_nightly_verdicts_latest ON mart.nightly_verdicts USING btree (check_name, ran_at DESC, recorded_at DESC); end if; end $$;
do $$ begin if to_regclass('mart.ix_question_coverage_report_universe') is null then CREATE INDEX ix_question_coverage_report_universe ON mart.question_coverage_report USING btree (universe_id, created_at DESC); end if; end $$;
do $$ begin if to_regclass('mart.idx_mart_research_card_artifacts_card_id') is null then CREATE INDEX idx_mart_research_card_artifacts_card_id ON mart.research_card_artifacts USING btree (card_id); end if; end $$;
do $$ begin if to_regclass('mart.idx_strategy_decisions_issuer_cutoff') is null then CREATE INDEX idx_strategy_decisions_issuer_cutoff ON mart.strategy_decisions USING btree (issuer_id, cutoff_at); end if; end $$;
do $$ begin if to_regclass('mart.idx_strategy_run_capture_bindings_strategy_run') is null then CREATE INDEX idx_strategy_run_capture_bindings_strategy_run ON mart.strategy_run_capture_bindings USING btree (strategy_run_id); end if; end $$;
do $$ begin if to_regclass('mart.idx_strategy_runs_strategy_key_order') is null then CREATE INDEX idx_strategy_runs_strategy_key_order ON mart.strategy_runs USING btree (strategy_key, executed_at DESC, created_at DESC, strategy_run_id DESC); end if; end $$;
do $$ begin if to_regclass('mart.idx_current_pointer_target_run') is null then CREATE INDEX idx_current_pointer_target_run ON mart.current_pointer USING btree (target_run_id); end if; end $$;
do $$ begin if to_regclass('mart.idx_current_pointer_previous_run') is null then CREATE INDEX idx_current_pointer_previous_run ON mart.current_pointer USING btree (previous_run_id) WHERE (previous_run_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('mart.uq_mart_entity_identity') is null then CREATE UNIQUE INDEX uq_mart_entity_identity ON mart.entity_identity USING btree (entity_id) NULLS NOT DISTINCT; end if; end $$;
do $$ begin if to_regclass('mart.ix_mart_entity_identity_listing') is null then CREATE INDEX ix_mart_entity_identity_listing ON mart.entity_identity USING btree (listing_id) WHERE (listing_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('mart.uq_mart_fund_holdings') is null then CREATE UNIQUE INDEX uq_mart_fund_holdings ON mart.fund_holdings USING btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) NULLS NOT DISTINCT; end if; end $$;
do $$ begin if to_regclass('mart.uq_mart_fund_holdings_resolved') is null then CREATE UNIQUE INDEX uq_mart_fund_holdings_resolved ON mart.fund_holdings_resolved USING btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) NULLS NOT DISTINCT; end if; end $$;
do $$ begin if to_regclass('mart.uq_mart_fund_holdings_coverage') is null then CREATE UNIQUE INDEX uq_mart_fund_holdings_coverage ON mart.fund_holdings_coverage USING btree (fund_id, report_period, transaction_time) NULLS NOT DISTINCT; end if; end $$;
do $$ begin if to_regclass('mart.uq_mart_fund_holdings_valuation') is null then CREATE UNIQUE INDEX uq_mart_fund_holdings_valuation ON mart.fund_holdings_valuation USING btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) NULLS NOT DISTINCT; end if; end $$;

insert into mart.environment_identity (singleton, environment)
select true, 'production'
where not exists (select 1 from mart.environment_identity)
on conflict (singleton) do nothing;
with seed (limit_key, hours) as (
    values
        ('daily', 72),
        ('weekly', 336),
        ('quarterly', 720),
        ('withhold', 720)
),
restored as (
    update mart.freshness_limit as present
       set hours = seed.hours
      from seed
     where present.limit_key = seed.limit_key
       and present.hours is distinct from seed.hours
)
insert into mart.freshness_limit (limit_key, hours)
select seed.*
from seed
where not exists (select 1 from mart.freshness_limit as present where present.limit_key = seed.limit_key)
on conflict (limit_key) do nothing;
with seed (artifact_key, lane, schedule_name, universe_like, family, served_to, wired) as (
    values
    ('head:topt', 'capture', 'topt_live_schedule', 'universe:topt-%', 'daily', 'consumers', true),
    ('head:qqq', 'capture', 'qqq_live_schedule', 'universe:qqq-%', 'daily', 'consumers', true),
    ('head:sp500', 'capture', 'sp500_live_schedule', 'universe:sp500-%', 'daily', 'consumers', true),
    ('head:canary', 'capture', 'canary_daily_schedule', 'universe:canary-%', 'daily', 'operators', true),
    ('head-reports', 'standards', 'head_reports_schedule', null, 'daily', 'operators', false),
    ('standards-backfill', 'standards', 'standard_backfill_schedule', null, 'weekly', 'consumers', false),
    ('standards-history', 'standards', 'standard_history_schedule', null, 'weekly', 'consumers', false),
    ('standards-nport-monthly', 'standards', 'nport_monthly_schedule', null, 'quarterly', 'consumers', false),
    ('standards-13f-quarterly', 'standards', '13f_quarterly_schedule', null, 'quarterly', 'consumers', false),
    ('standards-form4-daily', 'standards', 'form4_daily_schedule', null, 'daily', 'consumers', false),
    ('standards-10k-annual', 'standards', '10k_annual_schedule', null, 'weekly', 'consumers', false),
    ('universe-refresh', 'universe_refresh', 'universe_refresh_schedule', null, 'weekly', 'consumers', false),
    ('sp500-universe-refresh', 'universe_refresh', 'sp500_universe_refresh_schedule', null, 'weekly', 'consumers', false),
    -- market-data: the lane runs on weekdays only (cron `15 21 * * 1-5`).
    -- After a long weekend the age can pass 72 hours. Decide the family again when it is wired.
    ('market-data', 'market_data', 'market_data_refresh_schedule', null, 'daily', 'internal', false),
    -- qqq-prices: both daily series of the QQQ listings outside TOPT, on weekdays (cron `35 21 * * 1-5`, #1277).
    -- Like market-data, decide the family again when it is wired.
    ('qqq-prices', 'qqq_prices', 'qqq_price_refresh_schedule', null, 'daily', 'internal', false),
    -- macro-series: the U.S. Treasury par yields (#1255), loaded every day at 20:30 UTC (cron `30 20 * * *`).
    -- The Treasury publishes on business days, so a run on a weekend appends nothing. Internal until wired.
    ('macro-series', 'macro_data', 'macro_data_refresh_schedule', null, 'daily', 'internal', false),
    -- strategy-history: the lane runs on Sundays (cron `7 10 * * 0`) and reads stored bytes only.
    ('strategy-history', 'strategy_history', 'strategy_history_schedule', null, 'weekly', 'internal', false),
    -- backtest: the lane runs on weekdays (cron `30 21 * * 1-5`) and simulates daily/monthly portfolios.
    ('nightly-backtest', 'backtest', 'nightly_backtest_schedule', null, 'daily', 'internal', false),
    ('output-invariants', 'quality', 'output_invariants_schedule', null, 'daily', 'operators', false),
    ('datahub-confidence-report', 'quality', 'datahub_confidence_report_schedule', null, 'daily', 'operators', false),
    ('model-key-health', 'quality', 'model_key_health_schedule', null, 'daily', 'operators', false),
    ('job-run-health', 'quality', 'job_run_health_schedule', null, 'daily', 'operators', false),
    ('topt-baseline', 'quality', 'topt_baseline_schedule', null, 'daily', 'operators', false),
    ('entity-identity', 'entity_identity', null, null, 'weekly', 'consumers', false),
    ('triggers', 'triggers', null, null, 'daily', 'internal', false),
    ('facts:filing-derived', 'capture', null, null, 'quarterly', 'consumers', false)
),
restored as (
    update mart.served_artifact as present
       set lane = seed.lane,
           schedule_name = seed.schedule_name,
           universe_like = seed.universe_like,
           family = seed.family,
           served_to = seed.served_to,
           wired = seed.wired
      from seed
     where present.artifact_key = seed.artifact_key
       and (present.lane, present.schedule_name, present.universe_like,
            present.family, present.served_to, present.wired)
           is distinct from
           (seed.lane, seed.schedule_name, seed.universe_like,
            seed.family, seed.served_to, seed.wired)
)
insert into mart.served_artifact (artifact_key, lane, schedule_name, universe_like, family, served_to, wired)
select seed.*
from seed
where not exists (select 1 from mart.served_artifact as present where present.artifact_key = seed.artifact_key)
on conflict (artifact_key) do nothing;

do $$
declare
    tbl text;
    cname text;
begin
    foreach tbl in array array['mart.metric_node_values', 'mart.strategy_decisions'] loop
        if not exists (
            select 1
            from pg_constraint con
            join pg_attribute att
              on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
            where con.conrelid = tbl::regclass
              and con.contype = 'c'
              and att.attname = 'availability_status'
              and not att.attisdropped
              and pg_get_constraintdef(con.oid) like '%not_applicable%'
        ) then
            select con.conname into cname
            from pg_constraint con
            join pg_attribute att
              on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
            where con.conrelid = tbl::regclass
              and con.contype = 'c'
              and att.attname = 'availability_status'
              and not att.attisdropped
            limit 1;
            if cname is not null then
                execute format('alter table %s drop constraint %I', tbl, cname);
            end if;
            execute format(
                'alter table %s add constraint %I check (availability_status in (''available'', ''unavailable'', ''stale'', ''excluded'', ''low_confidence'', ''not_applicable'', ''error''))',
                tbl,
                split_part(tbl, '.', 2) || '_availability_status_check'
            );
        end if;
    end loop;
end $$;

do $$ begin if to_regclass('mart.universe_heads') is null then
execute $vc$create or replace view mart.universe_heads as
 select r.kind,
    r.sequence,
    coalesce(r.note, ''::text) as note,
    r.advanced_at,
    (o.payload ->> 'universe_id'::text) as universe_id,
    ((o.payload ->> 'instrument_count'::text))::integer as instrument_count,
    (o.payload ->> 'instrument_mapping_sha256'::text) as mapping_sha256
   from staging.accepted_rulesets r
     join staging.contract_objects o on o.contract_id = r.contract_id$vc$;
end if; end $$;

do $$ begin if to_regclass('mart.universe_constituents') is null then
execute $vc$create or replace view mart.universe_constituents as
 select f.etf_symbol,
    f.ticker,
    f.company_name,
    f.cik,
    f.figi,
    f.market_cap,
    f.as_of,
    f.source,
    r.payload_sha256 as fetch_sha256,
    r.object_uri
   from staging.etf_constituent_facts f
     join raw.fetches r on r.id = f.raw_fetch_id
  where f.as_of = (
    select max(f2.as_of)
      from staging.etf_constituent_facts f2
     where f2.etf_symbol = f.etf_symbol
  )$vc$;
end if; end $$;
