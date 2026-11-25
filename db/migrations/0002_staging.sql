-- idempotent: true
-- Canonical staging schema baseline (Wave 2 squash).
set lock_timeout = '5s';
set statement_timeout = '30s';

create schema if not exists staging;

CREATE OR REPLACE FUNCTION staging.entity_uuid_v5(p_namespace uuid, p_name text)
 RETURNS uuid
 LANGUAGE plpgsql
 IMMUTABLE PARALLEL SAFE STRICT
 SET search_path TO 'pg_catalog', 'public'
AS $function$
declare
    v_bytes bytea := substring(digest(uuid_send(p_namespace) || convert_to(p_name, 'UTF8'), 'sha1') from 1 for 16);
begin
    v_bytes := set_byte(v_bytes, 6, (get_byte(v_bytes, 6) & 15) | 80);
    v_bytes := set_byte(v_bytes, 8, (get_byte(v_bytes, 8) & 63) | 128);
    return encode(v_bytes, 'hex')::uuid;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_uuid_namespace(p_label text)
 RETURNS uuid
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE STRICT
AS $function$
    select staging.entity_uuid_v5(
        staging.entity_uuid_v5(
            '6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
            'https://github.com/wangzitian0/truealpha/blob/main/docs/entity-identity.md'),
        p_label);
$function$;

create table if not exists staging.accepted_rulesets (
 kind text not null,
 contract_id text not null,
 sequence integer not null,
 note text not null,
 advanced_at timestamp with time zone default clock_timestamp() not null,
 constraint accepted_rulesets_pkey PRIMARY KEY (kind, sequence),
 constraint accepted_rulesets_contract_id_check CHECK ((contract_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'::text)),
 constraint accepted_rulesets_kind_check CHECK ((kind <> ''::text)),
 constraint accepted_rulesets_note_check CHECK ((note <> ''::text)),
 constraint accepted_rulesets_sequence_check CHECK ((sequence > 0))
);

create table if not exists staging.api_call_ledger (
 id bigint generated always as identity,
 source text not null,
 endpoint text not null,
 called_at timestamp with time zone default now() not null,
 caller text not null,
 ok boolean not null,
 status_code integer,
 error text,
 duration_ms integer,
 request_uri text,
 payload_sha256 text,
 byte_length bigint,
 cost numeric default 1 not null,
 capacity_window_id text,
 run_key text,
 attempt smallint,
 provider_code text,
 request_params jsonb,
 completion_tokens integer,
 reasoning_tokens integer,
 fetch_key text,
 repeat_reason text,
 constraint api_call_ledger_pkey PRIMARY KEY (id),
 constraint api_call_ledger_cost_check CHECK ((cost >= (0)::numeric)),
 constraint api_call_ledger_payload_sha256_check CHECK (((payload_sha256 IS NULL) OR (payload_sha256 ~ '^[0-9a-f]{64}$'::text)))
);

create table if not exists staging.capture_normalized_observations (
 observation_id text not null,
 content_sha256 text not null,
 capture_obligation_id text not null,
 source_vintage_id text not null,
 semantic_type text not null,
 semantic_version text not null,
 subject_kind text not null,
 subject_id text not null,
 valid_from timestamp with time zone not null,
 valid_to timestamp with time zone,
 knowable_at timestamp with time zone not null,
 parser_version text not null,
 mapping_version text not null,
 normalized_payload_sha256 text not null,
 is_restatement boolean not null,
 supersedes_observation_id text,
 confidence numeric,
 freshness_state text default 'unknown'::text not null,
 payload jsonb not null,
 recorded_at timestamp with time zone default now() not null,
 constraint capture_normalized_observations_pkey PRIMARY KEY (observation_id),
 constraint capture_normalized_observations_check CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
 constraint capture_normalized_observations_check1 CHECK ((recorded_at >= knowable_at)),
 constraint capture_normalized_observations_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint capture_normalized_observations_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_normalized_observations_freshness_state_check CHECK ((freshness_state = ANY (ARRAY['fresh'::text, 'stale'::text, 'unknown'::text]))),
 constraint capture_normalized_observations_normalized_payload_sha256_check CHECK ((normalized_payload_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_normalized_observations_observation_id_check CHECK ((observation_id ~ '^normalized-observation:[0-9a-f]{64}$'::text)),
 constraint capture_normalized_observations_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_normalized_observations_capture_obligation_id_fkey FOREIGN KEY (capture_obligation_id) REFERENCES raw.capture_obligations(obligation_id),
 constraint capture_normalized_observations_source_vintage_id_fkey FOREIGN KEY (source_vintage_id) REFERENCES raw.capture_source_vintages(source_vintage_id),
 constraint capture_normalized_observations_supersedes_observation_id_fkey FOREIGN KEY (supersedes_observation_id) REFERENCES staging.capture_normalized_observations(observation_id)
);

create table if not exists staging.contract_objects (
 contract_id text not null,
 contract_kind text not null,
 content_sha256 text not null,
 payload jsonb not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint contract_objects_pkey PRIMARY KEY (contract_id),
 constraint contract_objects_check CHECK ((split_part(contract_id, ':'::text, 2) = content_sha256)),
 constraint contract_objects_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint contract_objects_contract_id_check CHECK ((contract_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'::text)),
 constraint contract_objects_kind_identity_check CHECK ((((contract_kind = 'registry_snapshot'::text) AND (contract_id ~~ 'registry-snapshot:%'::text)) OR ((contract_kind = 'research_catalog_manifest'::text) AND (contract_id ~~ 'research-catalog:%'::text)) OR ((contract_kind = 'snapshot_manifest'::text) AND (contract_id ~~ 'snapshot:%'::text)) OR ((contract_kind = 'release_manifest'::text) AND (contract_id ~~ 'release-manifest:%'::text)) OR ((contract_kind = 'capture_scope'::text) AND (contract_id ~~ 'capture-scope:%'::text)) OR ((contract_kind = 'capture_manifest'::text) AND (contract_id ~~ 'capture-manifest:%'::text)) OR ((contract_kind = 'capture_evaluation_report'::text) AND (contract_id ~~ 'capture-evaluation:%'::text)) OR ((contract_kind = 'trace_bundle'::text) AND (contract_id ~~ 'trace-bundle:%'::text)) OR ((contract_kind = 'strategy_usage_audit'::text) AND (contract_id ~~ 'strategy-usage-audit:%'::text)) OR ((contract_kind = 'graduation_attestation'::text) AND (contract_id ~~ 'graduation-attestation:%'::text)) OR ((contract_kind = 'usage_frequency_slice'::text) AND (contract_id ~~ 'usage-frequency:%'::text)) OR ((contract_kind = 'strategy_data_quality_review'::text) AND (contract_id ~~ 'strategy-data-quality-review:%'::text)) OR ((contract_kind = 'concept-mapping'::text) AND (contract_id ~~ 'concept-mapping:%'::text)) OR ((contract_kind ~~ 'universe-list:%'::text) AND (contract_id ~~ 'universe-list:%'::text)))),
 constraint contract_objects_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text))
);

create table if not exists staging.entity_kinds (
 kind text not null,
 parent_kind text,
 is_abstract boolean not null,
 uuid_namespace uuid,
 description text not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint entity_kinds_pkey PRIMARY KEY (kind),
 constraint entity_kinds_uuid_namespace_key UNIQUE (uuid_namespace),
 constraint entity_kind_namespace CHECK (((is_abstract AND (uuid_namespace IS NULL)) OR ((NOT is_abstract) AND (uuid_namespace = staging.entity_uuid_namespace(('kind:'::text || kind)))))),
 constraint entity_kind_single_root CHECK (((parent_kind IS NULL) = (kind = 'entity'::text))),
 constraint entity_kinds_description_check CHECK ((length(description) > 0)),
 constraint entity_kinds_kind_check CHECK ((kind ~ '^[a-z][a-z_]*$'::text)),
 constraint entity_kinds_parent_kind_fkey FOREIGN KEY (parent_kind) REFERENCES staging.entity_kinds(kind)
);

create table if not exists staging.etf_constituent_facts (
 id bigint generated always as identity,
 etf_symbol text not null,
 as_of date not null,
 source text not null,
 raw_fetch_id bigint not null,
 ticker text not null,
 company_name text default ''::text not null,
 weight numeric,
 market_cap numeric,
 cik integer,
 figi text,
 knowable_at timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 last_sale_price numeric,
 constraint etf_constituent_facts_pkey PRIMARY KEY (id),
 constraint etf_constituent_facts_etf_symbol_check CHECK ((etf_symbol <> ''::text)),
 constraint etf_constituent_facts_last_sale_price_check CHECK (((last_sale_price IS NULL) OR (last_sale_price >= (0)::numeric))),
 constraint etf_constituent_facts_source_check CHECK ((source <> ''::text)),
 constraint etf_constituent_facts_ticker_check CHECK ((ticker <> ''::text)),
 constraint etf_constituent_facts_raw_fetch_id_fkey FOREIGN KEY (raw_fetch_id) REFERENCES raw.fetches(id)
);

create table if not exists staging.evidence_nodes (
 node_id text not null,
 kind text not null,
 content_sha256 text not null,
 valid_from date not null,
 valid_to date,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone not null,
 supersedes_node_id text,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint evidence_nodes_pkey PRIMARY KEY (node_id),
 constraint evidence_node_hash_matches_id CHECK ((split_part(node_id, ':'::text, 2) = content_sha256)),
 constraint evidence_node_id_shape CHECK ((node_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'::text)),
 constraint evidence_node_kind CHECK ((kind = ANY (ARRAY['raw_fetch'::text, 'source_vintage'::text, 'normalized_observation'::text, 'snapshot'::text, 'factor_invocation'::text, 'materialized_result'::text, 'capture_run'::text, 'obligation'::text, 'quality_cell'::text, 'release_manifest'::text]))),
 constraint evidence_node_kind_prefix CHECK ((split_part(node_id, ':'::text, 1) =
CASE kind
    WHEN 'raw_fetch'::text THEN 'raw-fetch'::text
    WHEN 'source_vintage'::text THEN 'source-vintage'::text
    WHEN 'normalized_observation'::text THEN 'normalized-observation'::text
    WHEN 'snapshot'::text THEN 'snapshot'::text
    WHEN 'factor_invocation'::text THEN 'factor-invocation'::text
    WHEN 'materialized_result'::text THEN 'materialized-result'::text
    WHEN 'capture_run'::text THEN 'capture-run'::text
    WHEN 'obligation'::text THEN 'capture-obligation'::text
    WHEN 'quality_cell'::text THEN 'datahub-quality-cell'::text
    WHEN 'release_manifest'::text THEN 'release-manifest'::text
    ELSE NULL::text
END)),
 constraint evidence_node_no_self_supersede CHECK (((supersedes_node_id IS NULL) OR (supersedes_node_id <> node_id))),
 constraint evidence_node_period CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
 constraint evidence_nodes_supersedes_node_id_fkey FOREIGN KEY (supersedes_node_id) REFERENCES staging.evidence_nodes(node_id)
);

create table if not exists staging.ingestion_health_log (
 id bigint generated always as identity,
 source text not null,
 metric text not null,
 value numeric,
 logged_at timestamp with time zone default now() not null,
 note text,
 constraint ingestion_health_log_pkey PRIMARY KEY (id)
);

create table if not exists staging.issuer_cik_predecessors (
 issuer_id text not null,
 predecessor_cik bigint not null,
 reason text not null,
 approved_by text not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint issuer_cik_predecessors_pkey PRIMARY KEY (issuer_id),
 constraint issuer_cik_predecessors_approved_by_check CHECK ((length(approved_by) > 0)),
 constraint issuer_cik_predecessors_issuer_id_check CHECK ((issuer_id ~~ 'issuer:%'::text)),
 constraint issuer_cik_predecessors_predecessor_cik_check CHECK ((predecessor_cik > 0)),
 constraint issuer_cik_predecessors_reason_check CHECK ((length(reason) > 0))
);

create table if not exists staging.issuer_headcount_facts (
 id bigint generated always as identity,
 cik integer not null,
 headcount numeric not null,
 knowable_at timestamp with time zone not null,
 period_end date,
 source text not null,
 evidence_ref text not null,
 confidence numeric not null,
 recorded_at timestamp with time zone default now() not null,
 constraint issuer_headcount_facts_pkey PRIMARY KEY (id),
 constraint issuer_headcount_facts_check CHECK ((recorded_at >= (knowable_at - '400 days'::interval))),
 constraint issuer_headcount_facts_cik_check CHECK ((cik > 0)),
 constraint issuer_headcount_facts_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint issuer_headcount_facts_evidence_ref_check CHECK ((evidence_ref <> ''::text)),
 constraint issuer_headcount_facts_headcount_check CHECK ((headcount > (0)::numeric)),
 constraint issuer_headcount_facts_source_check CHECK ((source <> ''::text))
);

create table if not exists staging.issuer_segment_revenue_facts (
 id bigint generated always as identity,
 cik integer not null,
 segment_name text not null,
 segment_revenue numeric not null,
 partition_id text not null,
 partition_total numeric not null,
 partition_residual numeric not null,
 knowable_at timestamp with time zone not null,
 period_end date not null,
 source text not null,
 evidence_ref text not null,
 extractor text not null,
 confidence numeric not null,
 recorded_at timestamp with time zone default now() not null,
 constraint issuer_segment_revenue_facts_pkey PRIMARY KEY (id),
 constraint issuer_segment_revenue_facts_partition_id_segment_name_key UNIQUE (partition_id, segment_name),
 constraint issuer_segment_revenue_facts_check CHECK ((recorded_at >= (knowable_at - '400 days'::interval))),
 constraint issuer_segment_revenue_facts_cik_check CHECK ((cik > 0)),
 constraint issuer_segment_revenue_facts_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint issuer_segment_revenue_facts_evidence_ref_check CHECK ((evidence_ref <> ''::text)),
 constraint issuer_segment_revenue_facts_extractor_check CHECK ((extractor <> ''::text)),
 constraint issuer_segment_revenue_facts_partition_id_check CHECK ((partition_id ~ '^segment-partition:[0-9a-f]{64}$'::text)),
 constraint issuer_segment_revenue_facts_segment_name_check CHECK ((segment_name <> ''::text)),
 constraint issuer_segment_revenue_facts_source_check CHECK ((source <> ''::text))
);

create table if not exists staging.landed_fetch_keys (
 seat text not null,
 endpoint text not null,
 canonical_entity_id text not null,
 as_of text not null,
 raw_fetch_id bigint not null,
 payload_sha256 text not null,
 landed_at timestamp with time zone default clock_timestamp() not null,
 landed_run_key text,
 reuse_count bigint default 0 not null,
 last_reused_at timestamp with time zone,
 constraint landed_fetch_keys_pkey PRIMARY KEY (seat, endpoint, canonical_entity_id, as_of),
 constraint landed_fetch_keys_as_of_check CHECK ((char_length(as_of) > 0)),
 constraint landed_fetch_keys_canonical_entity_id_check CHECK ((char_length(canonical_entity_id) > 0)),
 constraint landed_fetch_keys_endpoint_check CHECK ((char_length(endpoint) > 0)),
 constraint landed_fetch_keys_payload_sha256_check CHECK ((payload_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint landed_fetch_keys_reuse_count_check CHECK ((reuse_count >= 0)),
 constraint landed_fetch_keys_seat_check CHECK ((char_length(seat) > 0)),
 constraint landed_fetch_keys_raw_fetch_id_fkey FOREIGN KEY (raw_fetch_id) REFERENCES raw.fetches(id)
);

create table if not exists staging.market_prices_daily (
 id bigint generated always as identity,
 symbol text not null,
 trading_date date not null,
 open numeric,
 high numeric,
 low numeric,
 close numeric,
 volume numeric,
 source text default 'twelvedata'::text not null,
 adjust text not null,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 confidence numeric not null,
 raw_ref text not null,
 constraint market_prices_daily_pkey PRIMARY KEY (id),
 constraint market_prices_daily_check CHECK ((recorded_at >= transaction_time)),
 constraint market_prices_daily_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);

create table if not exists staging.model_invocations (
 id bigint generated always as identity,
 invocation_id text not null,
 provider text not null,
 model text not null,
 base_url_host text not null,
 standard text not null,
 subject_cik integer not null,
 accession text not null,
 prompt_version text not null,
 prompt_sha256 text not null,
 schema_sha256 text not null,
 request_sha256 text not null,
 response_sha256 text,
 status_code integer,
 prompt_tokens integer,
 completion_tokens integer,
 cost numeric default 0 not null,
 decision jsonb not null,
 request jsonb not null,
 response jsonb,
 started_at timestamp with time zone not null,
 completed_at timestamp with time zone not null,
 recorded_at timestamp with time zone default now() not null,
 served_model text,
 constraint model_invocations_pkey PRIMARY KEY (id),
 constraint model_invocations_invocation_id_key UNIQUE (invocation_id),
 constraint model_invocations_accession_check CHECK ((accession <> ''::text)),
 constraint model_invocations_base_url_host_check CHECK ((base_url_host <> ''::text)),
 constraint model_invocations_check CHECK ((completed_at >= started_at)),
 constraint model_invocations_completion_tokens_check CHECK (((completion_tokens IS NULL) OR (completion_tokens >= 0))),
 constraint model_invocations_cost_check CHECK ((cost >= (0)::numeric)),
 constraint model_invocations_invocation_id_check CHECK ((invocation_id ~ '^model-invocation:[0-9a-f]{64}$'::text)),
 constraint model_invocations_model_check CHECK ((model <> ''::text)),
 constraint model_invocations_prompt_sha256_check CHECK ((prompt_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint model_invocations_prompt_tokens_check CHECK (((prompt_tokens IS NULL) OR (prompt_tokens >= 0))),
 constraint model_invocations_prompt_version_check CHECK ((prompt_version <> ''::text)),
 constraint model_invocations_provider_check CHECK ((provider <> ''::text)),
 constraint model_invocations_request_sha256_check CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint model_invocations_response_sha256_check CHECK (((response_sha256 IS NULL) OR (response_sha256 ~ '^[0-9a-f]{64}$'::text))),
 constraint model_invocations_schema_sha256_check CHECK ((schema_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint model_invocations_standard_check CHECK ((standard <> ''::text)),
 constraint model_invocations_subject_cik_check CHECK ((subject_cik > 0))
);

create table if not exists staging.pipeline_trigger_requests (
 request_id bigint generated always as identity,
 job_name text not null,
 executed_at timestamp with time zone not null,
 requested_by text not null,
 dedupe_key text not null,
 requested_at timestamp with time zone default clock_timestamp() not null,
 consumed_at timestamp with time zone,
 launched_run_key text,
 force_fetch boolean default false not null,
 constraint pipeline_trigger_requests_pkey PRIMARY KEY (request_id),
 constraint pipeline_trigger_requests_dedupe_key_key UNIQUE (dedupe_key),
 constraint pipeline_trigger_requests_check CHECK (((consumed_at IS NULL) = (launched_run_key IS NULL))),
 constraint pipeline_trigger_requests_job_name_check CHECK ((job_name = ANY (ARRAY['topt_live_pipeline'::text, 'qqq_live_pipeline'::text, 'sp500_live_pipeline'::text, 'canary_live_pipeline'::text]))),
 constraint pipeline_trigger_requests_requested_by_check CHECK ((length(requested_by) > 0))
);

create table if not exists staging.strategy_backtest_inputs (
 id bigint generated always as identity,
 issuer_id text not null,
 cutoff_at timestamp with time zone not null,
 input_key text not null,
 value numeric not null,
 confidence numeric not null,
 knowable_at timestamp with time zone not null,
 recorded_at timestamp with time zone default now() not null,
 fiscal_period text,
 constraint strategy_backtest_inputs_pkey PRIMARY KEY (id),
 constraint strategy_backtest_inputs_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint strategy_backtest_inputs_pit CHECK ((knowable_at <= cutoff_at))
);

create table if not exists staging.core_snapshots (
 snapshot_id text not null,
 content_sha256 text not null,
 run_id text not null,
 release_manifest_id text not null,
 universe_id text not null,
 universe_version text not null,
 universe_sha256 text not null,
 cutoff timestamp with time zone not null,
 issuer_count integer not null,
 instrument_count integer not null,
 observation_count integer not null,
 payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint core_snapshots_pkey PRIMARY KEY (snapshot_id),
 constraint core_snapshots_content_sha256_key UNIQUE (content_sha256),
 constraint core_snapshots_run_id_key UNIQUE (run_id),
 constraint core_snapshots_check CHECK ((split_part(snapshot_id, ':'::text, 2) = content_sha256)),
 constraint core_snapshots_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint core_snapshots_instrument_count_check CHECK ((instrument_count >= 1)),
 constraint core_snapshots_issuer_count_check CHECK (((issuer_count >= 1) AND (issuer_count <= instrument_count))),
 constraint core_snapshots_observation_count_check CHECK ((observation_count = (4 * instrument_count))),
 constraint core_snapshots_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint core_snapshots_snapshot_id_check CHECK ((snapshot_id ~ '^topt-core-snapshot:[0-9a-f]{64}$'::text)),
 constraint core_snapshots_universe_sha256_check CHECK ((universe_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint core_snapshots_run_id_fkey FOREIGN KEY (run_id) REFERENCES raw.capture_runs(run_id)
);

create table if not exists staging.capture_observation_obligations (
 capture_obligation_id text not null,
 observation_id text not null,
 bound_at timestamp with time zone default clock_timestamp() not null,
 constraint capture_observation_obligations_pkey PRIMARY KEY (capture_obligation_id, observation_id),
 constraint capture_observation_obligations_capture_obligation_id_fkey FOREIGN KEY (capture_obligation_id) REFERENCES raw.capture_obligations(obligation_id),
 constraint capture_observation_obligations_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES staging.capture_normalized_observations(observation_id)
);

create table if not exists staging.capture_observation_payloads (
 observation_id text not null,
 normalized_payload_sha256 text not null,
 normalized_payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint capture_observation_payloads_pkey PRIMARY KEY (observation_id),
 constraint capture_observation_payloads_normalized_payload_check CHECK ((jsonb_typeof(normalized_payload) = 'object'::text)),
 constraint capture_observation_payloads_normalized_payload_sha256_check CHECK ((normalized_payload_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_observation_payloads_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES staging.capture_normalized_observations(observation_id)
);

create table if not exists staging.entity_alias_schemes (
 scheme text not null,
 applies_to_kind text not null,
 is_unique boolean not null,
 value_reusable boolean not null,
 value_pattern text not null,
 authority text not null,
 description text not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint entity_alias_schemes_pkey PRIMARY KEY (scheme),
 constraint entity_alias_schemes_authority_check CHECK ((length(authority) > 0)),
 constraint entity_alias_schemes_description_check CHECK ((length(description) > 0)),
 constraint entity_alias_schemes_scheme_check CHECK ((scheme ~ '^[a-z][a-z0-9-]*$'::text)),
 constraint entity_alias_schemes_applies_to_kind_fkey FOREIGN KEY (applies_to_kind) REFERENCES staging.entity_kinds(kind)
);

create table if not exists staging.entity_relation_types (
 relation_type text not null,
 domain_kind text not null,
 range_kind text not null,
 uuid_namespace uuid not null,
 same_kind boolean not null,
 is_identity boolean not null,
 inverse_name text not null,
 description text not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 constraint entity_relation_types_pkey PRIMARY KEY (relation_type),
 constraint entity_relation_types_uuid_namespace_key UNIQUE (uuid_namespace),
 constraint entity_relation_type_namespace CHECK ((uuid_namespace = staging.entity_uuid_namespace(('relation:'::text || relation_type)))),
 constraint entity_relation_types_description_check CHECK ((length(description) > 0)),
 constraint entity_relation_types_inverse_name_check CHECK ((inverse_name ~ '^[a-z][a-z_]*$'::text)),
 constraint entity_relation_types_relation_type_check CHECK ((relation_type ~ '^[a-z][a-z_]*$'::text)),
 constraint entity_relation_types_domain_kind_fkey FOREIGN KEY (domain_kind) REFERENCES staging.entity_kinds(kind),
 constraint entity_relation_types_range_kind_fkey FOREIGN KEY (range_kind) REFERENCES staging.entity_kinds(kind)
);

create table if not exists staging.evidence_edges (
 edge_id text not null,
 content_sha256 text not null,
 from_kind text not null,
 from_id text not null,
 to_kind text not null,
 to_id text not null,
 relation text not null,
 valid_from date not null,
 valid_to date,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint evidence_edges_pkey PRIMARY KEY (edge_id),
 constraint evidence_edge_hash_matches_id CHECK ((split_part(edge_id, ':'::text, 2) = content_sha256)),
 constraint evidence_edge_id_shape CHECK ((edge_id ~ '^evidence-edge:[0-9a-f]{64}$'::text)),
 constraint evidence_edge_not_self CHECK ((from_id <> to_id)),
 constraint evidence_edge_period CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
 constraint evidence_edge_relation CHECK ((relation = ANY (ARRAY['derived_from'::text, 'selected_from'::text, 'member_of'::text, 'bound_to'::text, 'attested_by'::text, 'supersedes'::text]))),
 constraint evidence_edges_from_id_fkey FOREIGN KEY (from_id) REFERENCES staging.evidence_nodes(node_id),
 constraint evidence_edges_to_id_fkey FOREIGN KEY (to_id) REFERENCES staging.evidence_nodes(node_id)
);

create table if not exists staging.entities (
 entity_id uuid not null,
 kind text not null,
 mint_rule text not null,
 minted_at timestamp with time zone default clock_timestamp() not null,
 minted_by text not null,
 birth_scheme text not null,
 birth_value text not null,
 birth_generation integer default 1 not null,
 constraint entities_pkey PRIMARY KEY (entity_id),
 constraint entities_birth_generation_check CHECK ((birth_generation >= 1)),
 constraint entities_mint_rule_check CHECK ((mint_rule = 'uuidv5:v1'::text)),
 constraint entities_minted_by_check CHECK ((length(minted_by) > 0)),
 constraint entity_id_is_uuidv5 CHECK ((SUBSTRING((entity_id)::text FROM 15 FOR 1) = '5'::text)),
 constraint entities_birth_scheme_fkey FOREIGN KEY (birth_scheme) REFERENCES staging.entity_alias_schemes(scheme),
 constraint entities_kind_fkey FOREIGN KEY (kind) REFERENCES staging.entity_kinds(kind)
);

create table if not exists staging.fund_holding_facts (
 id bigint generated always as identity,
 fund_id uuid not null,
 holding_id uuid,
 holding_name text not null,
 report_period date not null,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone default now() not null,
 cusip text,
 isin text,
 lei text,
 balance numeric,
 value_usd numeric not null,
 percent_of_net_assets numeric not null,
 confidence numeric not null,
 raw_ref text not null,
 constraint fund_holding_facts_pkey PRIMARY KEY (id),
 constraint fund_holding_facts_check CHECK ((recorded_at >= transaction_time)),
 constraint fund_holding_facts_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint fund_holding_facts_fund_id_fkey FOREIGN KEY (fund_id) REFERENCES staging.entities(entity_id),
 constraint fund_holding_facts_holding_id_fkey FOREIGN KEY (holding_id) REFERENCES staging.entities(entity_id)
);

create table if not exists staging.core_snapshot_members (
 snapshot_id text not null,
 instrument_id text not null,
 issuer_id text not null,
 listing_id text not null,
 observation_ids text[] not null,
 member_sha256 text not null,
 factor_input jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint core_snapshot_members_pkey PRIMARY KEY (snapshot_id, issuer_id),
 constraint core_snapshot_members_snapshot_id_instrument_id_key UNIQUE (snapshot_id, instrument_id),
 constraint core_snapshot_members_snapshot_id_listing_id_key UNIQUE (snapshot_id, listing_id),
 constraint core_snapshot_members_factor_input_check CHECK ((jsonb_typeof(factor_input) = 'object'::text)),
 constraint core_snapshot_members_member_sha256_check CHECK ((member_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint core_snapshot_members_observation_ids_check CHECK (((cardinality(observation_ids) >= 4) AND ((cardinality(observation_ids) % 4) = 0))),
 constraint core_snapshot_members_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES staging.core_snapshots(snapshot_id)
);

create table if not exists staging.entity_aliases (
 alias_id bigint generated always as identity,
 entity_id uuid not null,
 scheme text not null,
 value text not null,
 valid_from date not null,
 valid_to date,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 source text not null,
 raw_ref text not null,
 method text not null,
 confidence numeric not null,
 evidence jsonb default '{}'::jsonb not null,
 mapping_version text not null,
 constraint entity_aliases_pkey PRIMARY KEY (alias_id),
 constraint entity_alias_vintage UNIQUE (scheme, value, entity_id, valid_from, transaction_time, method, source),
 constraint entity_alias_period CHECK (((valid_to IS NULL) OR (valid_to > valid_from))),
 constraint entity_alias_recorded_after_knowable CHECK ((recorded_at >= transaction_time)),
 constraint entity_aliases_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint entity_aliases_evidence_check CHECK ((jsonb_typeof(evidence) = 'object'::text)),
 constraint entity_aliases_mapping_version_check CHECK ((length(mapping_version) > 0)),
 constraint entity_aliases_method_check CHECK ((method = ANY (ARRAY['asserted'::text, 'parsed'::text, 'crosswalk'::text]))),
 constraint entity_aliases_raw_ref_check CHECK ((length(raw_ref) > 0)),
 constraint entity_aliases_source_check CHECK ((length(source) > 0)),
 constraint entity_aliases_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES staging.entities(entity_id),
 constraint entity_aliases_scheme_fkey FOREIGN KEY (scheme) REFERENCES staging.entity_alias_schemes(scheme)
);

create table if not exists staging.entity_relations (
 relation_id uuid not null,
 relation_type text not null,
 from_entity_id uuid not null,
 to_entity_id uuid not null,
 valid_from date not null,
 valid_to date,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 source text not null,
 raw_ref text not null,
 method text not null,
 confidence numeric not null,
 evidence jsonb default '{}'::jsonb not null,
 attributes jsonb default '{}'::jsonb not null,
 mapping_version text not null,
 constraint entity_relations_pkey PRIMARY KEY (relation_id),
 constraint entity_relation_id_is_uuidv5 CHECK ((SUBSTRING((relation_id)::text FROM 15 FOR 1) = '5'::text)),
 constraint entity_relation_not_self CHECK ((from_entity_id <> to_entity_id)),
 constraint entity_relation_period CHECK (((valid_to IS NULL) OR (valid_to > valid_from))),
 constraint entity_relation_recorded_after_knowable CHECK ((recorded_at >= transaction_time)),
 constraint entity_relations_attributes_check CHECK ((jsonb_typeof(attributes) = 'object'::text)),
 constraint entity_relations_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
 constraint entity_relations_evidence_check CHECK ((jsonb_typeof(evidence) = 'object'::text)),
 constraint entity_relations_mapping_version_check CHECK ((length(mapping_version) > 0)),
 constraint entity_relations_method_check CHECK ((method = ANY (ARRAY['asserted'::text, 'derived'::text, 'crosswalk'::text, 'merge'::text, 'split'::text]))),
 constraint entity_relations_raw_ref_check CHECK ((length(raw_ref) > 0)),
 constraint entity_relations_source_check CHECK ((length(source) > 0)),
 constraint entity_relations_from_entity_id_fkey FOREIGN KEY (from_entity_id) REFERENCES staging.entities(entity_id),
 constraint entity_relations_relation_type_fkey FOREIGN KEY (relation_type) REFERENCES staging.entity_relation_types(relation_type),
 constraint entity_relations_to_entity_id_fkey FOREIGN KEY (to_entity_id) REFERENCES staging.entities(entity_id)
);

create table if not exists staging.entity_retractions (
 retraction_id bigint generated always as identity,
 alias_id bigint,
 relation_id uuid,
 valid_to date not null,
 reason text not null,
 transaction_time timestamp with time zone not null,
 recorded_at timestamp with time zone default clock_timestamp() not null,
 source text not null,
 raw_ref text not null,
 constraint entity_retractions_pkey PRIMARY KEY (retraction_id),
 constraint entity_retraction_one_target CHECK ((num_nonnulls(alias_id, relation_id) = 1)),
 constraint entity_retraction_recorded_after_knowable CHECK ((recorded_at >= transaction_time)),
 constraint entity_retractions_raw_ref_check CHECK ((length(raw_ref) > 0)),
 constraint entity_retractions_reason_check CHECK ((length(reason) > 0)),
 constraint entity_retractions_source_check CHECK ((length(source) > 0)),
 constraint entity_retractions_alias_id_fkey FOREIGN KEY (alias_id) REFERENCES staging.entity_aliases(alias_id),
 constraint entity_retractions_relation_id_fkey FOREIGN KEY (relation_id) REFERENCES staging.entity_relations(relation_id)
);

CREATE OR REPLACE FUNCTION staging.entity_alias_normalize(p_scheme text, p_value text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
AS $function$
    select case
        when p_value is null then null
        when p_scheme in ('legacy-id', 'name') then btrim(p_value)
        when p_scheme = 'cik' and btrim(p_value) ~ '^[0-9]{1,10}$' then lpad(btrim(p_value), 10, '0')
        else upper(btrim(p_value))
    end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_alias_valid_to(p_alias_id bigint, p_known_at timestamp with time zone)
 RETURNS date
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
    select least(alias.valid_to, min(retraction.valid_to))
    from staging.entity_aliases alias
    left join staging.entity_retractions retraction
      on retraction.alias_id = alias.alias_id
     and retraction.transaction_time <= p_known_at
    where alias.alias_id = p_alias_id
    group by alias.valid_to;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_backfill()
 RETURNS jsonb
 LANGUAGE plpgsql
 SET jit TO 'off'
AS $function$
declare
    v_version constant text := 'entity-backfill:v1';
    v_started timestamptz := clock_timestamp();
    v_component record;
    v_claim record;
    v_birth_scheme text;
    v_birth_value text;
    v_existing uuid[];
    v_survivor uuid;
    v_loser uuid;
    v_merge_known timestamptz;
    v_merge_evidence jsonb;
    v_from uuid;
    v_to uuid;
    v_failed jsonb := '[]'::jsonb;
begin
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_backfill', 0));

    if to_regclass('pg_temp.entity_backfill_claims') is not null then
        drop table pg_temp.entity_backfill_claims;
    end if;
    create temporary table entity_backfill_claims on commit drop as
        select * from staging.entity_backfill_plan;
    create index on pg_temp.entity_backfill_claims (claim, kind, component);

    -- The registry, not the plan, knows which values are well formed and which schemes
    -- never change hands (those hold for all time once assigned).
    update pg_temp.entity_backfill_claims claim
       set status = case
               when claim.value is distinct from staging.entity_alias_normalize(claim.scheme, claim.value)
                 or claim.value !~ scheme.value_pattern
                 or not staging.entity_kind_is_a(claim.kind, scheme.applies_to_kind)
               then 'skipped:invalid-value'
               else claim.status
           end,
           valid_from = case when scheme.value_reusable then claim.valid_from else '-infinity'::date end
      from staging.entity_alias_schemes scheme
     where claim.claim = 'alias'
       and claim.status = 'planned'
       and scheme.scheme = claim.scheme;
    analyze pg_temp.entity_backfill_claims;

    if to_regclass('pg_temp.entity_backfill_components') is not null then
        drop table pg_temp.entity_backfill_components;
    end if;
    create temporary table entity_backfill_components (
        kind text not null,
        component text not null,
        entity_id uuid not null,
        primary key (kind, component)
    ) on commit drop;

    -- Every entity and the entity it survives as now: staging.entity_survivor(x, 'infinity')
    -- for all x in one pass, kept current below as entities are minted and merged.
    if to_regclass('pg_temp.entity_backfill_survivors') is not null then
        drop table pg_temp.entity_backfill_survivors;
    end if;
    create temporary table entity_backfill_survivors on commit drop as
    with recursive edge as (
        select distinct on (relation.from_entity_id) relation.from_entity_id, relation.to_entity_id
        from staging.entity_relations relation
        where relation.relation_type = 'superseded_by'
          and not exists (
              select 1 from staging.entity_retractions retraction
              where retraction.relation_id = relation.relation_id)
        order by relation.from_entity_id, relation.transaction_time desc, relation.relation_id desc
    ), walk(entity_id, current_id, depth) as (
        select entity.entity_id, entity.entity_id, 0 from staging.entities entity
        union all
        select walk.entity_id, edge.to_entity_id, walk.depth + 1
        from walk
        join edge on edge.from_entity_id = walk.current_id
        where walk.depth < 32
    )
    select distinct on (entity_id) entity_id, current_id as survivor_id
    from walk
    order by entity_id, depth desc;
    alter table pg_temp.entity_backfill_survivors add primary key (entity_id);
    create index on pg_temp.entity_backfill_survivors (survivor_id);
    analyze pg_temp.entity_backfill_survivors;

    for v_component in
        select kind, component
        from pg_temp.entity_backfill_claims
        where claim = 'alias' and status = 'planned'
        group by kind, component
        order by kind, component
    loop
        begin
            v_birth_scheme := null;
            v_birth_value := null;

            -- Entities that already hold one of this component's unique aliases over an
            -- overlapping validity, as the entities they survive as.
            select array_agg(distinct holder.survivor_id)
              into v_existing
            from pg_temp.entity_backfill_claims claim
            join staging.entity_alias_schemes scheme
              on scheme.scheme = claim.scheme and scheme.is_unique
            join staging.entity_aliases alias
              on alias.scheme = claim.scheme and alias.value = claim.value
            join pg_temp.entity_backfill_survivors holder
              on holder.entity_id = alias.entity_id
            cross join lateral (
                select coalesce(least(alias.valid_to, (
                           select min(retraction.valid_to) from staging.entity_retractions retraction
                           where retraction.alias_id = alias.alias_id)), 'infinity'::date) as effective_to
            ) held
            where claim.claim = 'alias' and claim.status = 'planned'
              and claim.kind = v_component.kind and claim.component = v_component.component
              and alias.valid_from < held.effective_to
              and claim.valid_from < held.effective_to;

            if exists (
                select 1 from staging.entities entity
                where entity.entity_id = any(coalesce(v_existing, '{}'::uuid[]))
                  and entity.kind <> v_component.kind
            ) then
                raise exception 'aliases of % already name an entity of another kind', v_component.component;
            end if;

            -- The birth alias comes from the component's claims alone, never from the clock
            -- or the load order: the pipeline's own legacy id that became knowable first,
            -- then the smallest value. Evidence that arrives later is almost always knowable
            -- later too, so a store that grows run by run picks the birth a store loaded at
            -- once picks.
            select claim.scheme, claim.value into v_birth_scheme, v_birth_value
            from pg_temp.entity_backfill_claims claim
            where claim.claim = 'alias' and claim.status = 'planned'
              and claim.kind = v_component.kind and claim.component = v_component.component
            order by (claim.scheme = 'legacy-id') desc, claim.transaction_time, claim.scheme, claim.value
            limit 1;

            if v_existing is null then
                v_survivor := staging.entity_mint(v_component.kind, v_birth_scheme, v_birth_value, v_version);
                insert into pg_temp.entity_backfill_survivors (entity_id, survivor_id)
                values (v_survivor, v_survivor);
            elsif not exists (
                select 1 from staging.entities entity
                where entity.entity_id = any(v_existing)
                  and entity.birth_value not like 'company:isin:%'
            ) then
                v_survivor := staging.entity_mint(v_component.kind, v_birth_scheme, v_birth_value, v_version);
                insert into pg_temp.entity_backfill_survivors (entity_id, survivor_id)
                values (v_survivor, v_survivor);
                select max(claim.transaction_time),
                       jsonb_build_object(
                           'component', v_component.component,
                           'crosswalk', coalesce(
                               jsonb_agg(claim.evidence) filter (where claim.method = 'crosswalk'),
                               '[]'::jsonb))
                  into v_merge_known, v_merge_evidence
                from pg_temp.entity_backfill_claims claim
                where claim.claim = 'alias' and claim.status = 'planned'
                  and claim.kind = v_component.kind and claim.component = v_component.component;
                for v_loser in
                    select entity.entity_id from staging.entities entity
                    where entity.entity_id = any(v_existing)
                    order by entity.birth_scheme, entity.birth_value, entity.birth_generation
                loop
                    insert into staging.entity_relations
                        (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                         transaction_time, source, raw_ref, method, confidence, evidence,
                         mapping_version)
                    select staging.entity_relation_uuid(
                               edge.relation_type, v_loser, v_survivor, '-infinity',
                               v_merge_known, 'entity-backfill', 'merge'),
                           edge.relation_type, v_loser, v_survivor, '-infinity',
                           v_merge_known, 'entity-backfill', v_component.component, 'merge', 1.0,
                           v_merge_evidence, v_version
                    from (values ('same_as'), ('superseded_by')) as edge (relation_type);
                    update pg_temp.entity_backfill_survivors
                       set survivor_id = v_survivor
                     where survivor_id = v_loser;
                end loop;
            else
                -- The survivor follows the same rule: the entity born from the component's
                -- birth alias, else the one whose birth alias became knowable first. A store
                -- that learned the link late then resolves every alias to the entity a store
                -- that knew it from the start minted.
                select entity.entity_id into v_survivor
                from staging.entities entity
                where entity.entity_id = any(v_existing)
                order by (entity.birth_scheme = v_birth_scheme and entity.birth_value = v_birth_value) desc,
                         (entity.birth_scheme = 'legacy-id') desc,
                         (select min(birth.transaction_time) from staging.entity_aliases birth
                          where birth.entity_id = entity.entity_id
                            and birth.scheme = entity.birth_scheme and birth.value = entity.birth_value),
                         entity.birth_scheme, entity.birth_value, entity.birth_generation
                limit 1;
                if cardinality(v_existing) > 1 then
                    select max(claim.transaction_time),
                           jsonb_build_object(
                               'component', v_component.component,
                               'crosswalk', coalesce(
                                   jsonb_agg(claim.evidence) filter (where claim.method = 'crosswalk'),
                                   '[]'::jsonb))
                      into v_merge_known, v_merge_evidence
                    from pg_temp.entity_backfill_claims claim
                    where claim.claim = 'alias' and claim.status = 'planned'
                      and claim.kind = v_component.kind and claim.component = v_component.component;
                    for v_loser in
                        select entity.entity_id from staging.entities entity
                        where entity.entity_id = any(v_existing) and entity.entity_id <> v_survivor
                        order by entity.birth_scheme, entity.birth_value, entity.birth_generation
                    loop
                        insert into staging.entity_relations
                            (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                             transaction_time, source, raw_ref, method, confidence, evidence,
                             mapping_version)
                        select staging.entity_relation_uuid(
                                   edge.relation_type, v_loser, v_survivor, '-infinity',
                                   v_merge_known, 'entity-backfill', 'merge'),
                               edge.relation_type, v_loser, v_survivor, '-infinity',
                               v_merge_known, 'entity-backfill', v_component.component, 'merge', 1.0,
                               v_merge_evidence, v_version
                        from (values ('same_as'), ('superseded_by')) as edge (relation_type);
                        update pg_temp.entity_backfill_survivors
                           set survivor_id = v_survivor
                         where survivor_id = v_loser;
                    end loop;
                end if;
            end if;

            for v_claim in
                select * from pg_temp.entity_backfill_claims claim
                where claim.claim = 'alias' and claim.status = 'planned'
                  and claim.kind = v_component.kind and claim.component = v_component.component
                order by (claim.scheme = v_birth_scheme and claim.value = v_birth_value) desc nulls last,
                         claim.transaction_time, claim.scheme, claim.value, claim.method, claim.source
            loop
                if exists (
                    select 1
                    from staging.entity_aliases alias
                    join pg_temp.entity_backfill_survivors holder
                      on holder.entity_id = alias.entity_id and holder.survivor_id = v_survivor
                    where alias.scheme = v_claim.scheme and alias.value = v_claim.value
                      and alias.method = v_claim.method and alias.source = v_claim.source
                      and alias.valid_from <= v_claim.valid_from
                      and alias.valid_to is null
                      and not exists (
                          select 1 from staging.entity_retractions retraction
                          where retraction.alias_id = alias.alias_id)
                ) then
                    continue;
                end if;
                insert into staging.entity_aliases
                    (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,
                     method, confidence, evidence, mapping_version)
                values
                    (v_survivor, v_claim.scheme, v_claim.value, v_claim.valid_from,
                     v_claim.transaction_time, v_claim.source, v_claim.raw_ref, v_claim.method,
                     v_claim.confidence, v_claim.evidence, v_version);
            end loop;

            insert into pg_temp.entity_backfill_components (kind, component, entity_id)
            values (v_component.kind, v_component.component, v_survivor);
        exception when others then
            v_failed := v_failed || jsonb_build_object(
                'component', v_component.component, 'kind', v_component.kind, 'error', sqlerrm);
        end;
    end loop;
    analyze pg_temp.entity_backfill_survivors;

    for v_claim in
        select * from pg_temp.entity_backfill_claims claim
        where claim.claim = 'relation' and claim.status = 'planned'
        order by claim.relation_type, claim.transaction_time, claim.component, claim.to_component
    loop
        select entity_id into v_from from pg_temp.entity_backfill_components
        where kind = v_claim.kind and component = v_claim.component;
        select entity_id into v_to from pg_temp.entity_backfill_components
        where kind = v_claim.to_kind and component = v_claim.to_component;
        if v_from is null or v_to is null then
            update pg_temp.entity_backfill_claims
               set status = 'skipped:endpoint-not-written'
             where claim = 'relation' and relation_type = v_claim.relation_type
               and component = v_claim.component and to_component = v_claim.to_component;
            continue;
        end if;
        -- One issuer per instrument and one instrument per listing, across the whole store:
        -- another endpoint blocks the claim only while its edge is still in force on or after
        -- the claim's first valid day (the claims here are open-ended). An edge retracted
        -- before then is history, and one withdrawn outright never held.
        if exists (
            select 1
            from pg_temp.entity_backfill_survivors target
            join staging.entity_relations relation
              on relation.to_entity_id = target.entity_id
             and relation.relation_type = v_claim.relation_type
            join pg_temp.entity_backfill_survivors origin
              on origin.entity_id = relation.from_entity_id
            cross join lateral (
                select coalesce(least(relation.valid_to, (
                           select min(retraction.valid_to) from staging.entity_retractions retraction
                           where retraction.relation_id = relation.relation_id)), 'infinity'::date) as effective_to
            ) held
            where target.survivor_id = v_to
              and origin.survivor_id <> v_from
              and relation.valid_from < held.effective_to
              and v_claim.valid_from < held.effective_to
        ) then
            update pg_temp.entity_backfill_claims
               set status = 'skipped:store-holds-another-endpoint'
             where claim = 'relation' and relation_type = v_claim.relation_type
               and component = v_claim.component and to_component = v_claim.to_component;
            continue;
        end if;
        if exists (
            select 1
            from pg_temp.entity_backfill_survivors target
            join staging.entity_relations relation
              on relation.to_entity_id = target.entity_id
             and relation.relation_type = v_claim.relation_type
            join pg_temp.entity_backfill_survivors origin
              on origin.entity_id = relation.from_entity_id and origin.survivor_id = v_from
            where target.survivor_id = v_to
              and relation.method = v_claim.method and relation.source = v_claim.source
              and relation.valid_from <= v_claim.valid_from
              and relation.valid_to is null
              and not exists (
                  select 1 from staging.entity_retractions retraction
                  where retraction.relation_id = relation.relation_id)
        ) then
            continue;
        end if;
        begin
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                 transaction_time, source, raw_ref, method, confidence, evidence, mapping_version)
            values
                (staging.entity_relation_uuid(
                     v_claim.relation_type, v_from, v_to, v_claim.valid_from,
                     v_claim.transaction_time, v_claim.source, v_claim.method),
                 v_claim.relation_type, v_from, v_to, v_claim.valid_from,
                 v_claim.transaction_time, v_claim.source, v_claim.raw_ref, v_claim.method,
                 v_claim.confidence, v_claim.evidence, v_version);
        exception when others then
            v_failed := v_failed || jsonb_build_object(
                'relation', v_claim.relation_type, 'from', v_claim.component,
                'to', v_claim.to_component, 'error', sqlerrm);
        end;
    end loop;

    -- What this call wrote, read back from the store rather than counted along the way (a
    -- rolled-back component leaves nothing to count).
    return jsonb_build_object(
        'minted', coalesce((
            select jsonb_object_agg(kind, written) from (
                select kind, count(*) as written from staging.entities
                where minted_by = v_version and minted_at >= v_started group by kind) minted), '{}'::jsonb),
        'aliases', coalesce((
            select jsonb_object_agg(scheme, written) from (
                select scheme, count(*) as written from staging.entity_aliases
                where mapping_version = v_version and recorded_at >= v_started group by scheme) aliases),
            '{}'::jsonb),
        'relations', coalesce((
            select jsonb_object_agg(relation_type, written) from (
                select relation_type, count(*) as written from staging.entity_relations
                where mapping_version = v_version and recorded_at >= v_started group by relation_type) relations),
            '{}'::jsonb),
        'held_back', coalesce((
            select jsonb_object_agg(status, claims) from (
                select status, count(*) as claims from pg_temp.entity_backfill_claims
                where status <> 'planned' group by status) held), '{}'::jsonb),
        'failed', v_failed);
end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_backfill_due(p_since timestamp with time zone)
 RETURNS text
 LANGUAGE sql
 STABLE
 SET jit TO 'off'
AS $function$
    with used(legacy_id) as (
        select distinct used.legacy_id
        from staging.capture_observation_payloads payload
        cross join lateral (values
            (case when jsonb_typeof(payload.normalized_payload->'issuer_id') = 'string'
                  then payload.normalized_payload->>'issuer_id' end),
            (case when jsonb_typeof(payload.normalized_payload->'instrument_id') = 'string'
                  then payload.normalized_payload->>'instrument_id' end),
            (case when jsonb_typeof(payload.normalized_payload->'listing_id') = 'string'
                  then payload.normalized_payload->>'listing_id' end)) as used(legacy_id)
        where used.legacy_id is not null
        union
        select member->>position
        from staging.contract_objects head
        cross join lateral jsonb_array_elements(
            case when jsonb_typeof(head.payload->'instruments') = 'array'
                 then head.payload->'instruments' else '[]'::jsonb end) member
        cross join (values (0), (1), (2)) as slot(position)
        where head.contract_kind like 'universe-list:%'
          and jsonb_typeof(member) = 'array'
          and jsonb_typeof(member->position) = 'string'
          and member->>position is not null
    )
    select case
        when not exists (select 1 from staging.entities) and exists (select 1 from used)
            then 'store-empty'
        when exists (
            select 1 from used
            where not exists (
                select 1 from staging.entity_aliases alias
                where alias.scheme = 'legacy-id' and alias.value = used.legacy_id))
            then 'unseen-legacy-id'
        when exists (
                select 1 from staging.fund_holding_facts
                where recorded_at > coalesce(p_since, '-infinity'::timestamptz))
          or exists (
                select 1 from staging.entity_aliases
                where scheme = 'isin'
                  and mapping_version not like 'entity-backfill%'
                  and recorded_at > coalesce(p_since, '-infinity'::timestamptz))
          or exists (
                select 1 from staging.entity_relations
                where mapping_version not like 'entity-backfill%'
                  and recorded_at > coalesce(p_since, '-infinity'::timestamptz))
            then 'new-crosswalk-evidence'
    end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_birth_name(p_scheme text, p_value text, p_generation integer)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE STRICT
AS $function$
    select p_scheme || ':' || p_value || case when p_generation > 1 then '#' || p_generation else '' end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_evidence_watermark()
 RETURNS timestamp with time zone
 LANGUAGE sql
 STABLE
 SET jit TO 'off'
AS $function$
    select greatest(
        (select max(recorded_at) from staging.capture_normalized_observations),
        (select max(recorded_at) from staging.contract_objects where contract_kind like 'universe-list:%'),
        (select max(recorded_at) from staging.fund_holding_facts),
        (select max(recorded_at) from staging.entity_aliases where scheme = 'isin' and mapping_version not like 'entity-backfill%'),
        (select max(recorded_at) from staging.entity_relations where mapping_version not like 'entity-backfill%'));
$function$;
CREATE OR REPLACE FUNCTION staging.entity_kind_is_a(p_kind text, p_ancestor text)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE
AS $function$
declare
    v_kind text := p_kind;
    v_depth integer := 0;
begin
    while v_kind is not null and v_depth <= 16 loop
        if v_kind = p_ancestor then
            return true;
        end if;
        select parent_kind into v_kind from staging.entity_kinds where kind = v_kind;
        v_depth := v_depth + 1;
    end loop;
    return false;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_mint(p_kind text, p_scheme text, p_value text, p_minted_by text)
 RETURNS uuid
 LANGUAGE plpgsql
AS $function$
declare
    v_namespace uuid;
    v_value text := staging.entity_alias_normalize(p_scheme, p_value);
    v_generation integer := 1;
    v_id uuid;
begin
    select uuid_namespace into v_namespace from staging.entity_kinds where kind = p_kind;
    if v_namespace is null then
        raise exception 'entity kind % cannot be minted', p_kind;
    end if;
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_mint:' || p_kind || ':' || p_scheme || ':' || v_value, 0));
    loop
        v_id := staging.entity_uuid_v5(v_namespace, staging.entity_birth_name(p_scheme, v_value, v_generation));
        exit when not exists (select 1 from staging.entities where entity_id = v_id);
        v_generation := v_generation + 1;
    end loop;
    insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value, birth_generation)
    values (v_id, p_kind, 'uuidv5:v1', p_minted_by, p_scheme, v_value, v_generation);
    return v_id;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_name_date(p_date date)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE STRICT
AS $function$
    select case when isfinite(p_date) then to_char(p_date, 'YYYY-MM-DD') else p_date::text end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_name_time(p_time timestamp with time zone)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE STRICT
AS $function$
    select to_char(p_time at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
$function$;
CREATE OR REPLACE FUNCTION staging.entity_relation_uuid(p_relation_type text, p_from uuid, p_to uuid, p_valid_from date, p_transaction_time timestamp with time zone, p_source text, p_method text)
 RETURNS uuid
 LANGUAGE sql
 STABLE
AS $function$
    select staging.entity_uuid_v5(
        relation_type.uuid_namespace,
        concat_ws('|', p_from, p_to, staging.entity_name_date(p_valid_from),
                  staging.entity_name_time(p_transaction_time), p_source, p_method))
    from staging.entity_relation_types relation_type
    where relation_type.relation_type = p_relation_type;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_relation_valid_to(p_relation_id uuid, p_known_at timestamp with time zone)
 RETURNS date
 LANGUAGE sql
 STABLE
AS $function$
    select least(relation.valid_to, min(retraction.valid_to))
    from staging.entity_relations relation
    left join staging.entity_retractions retraction
      on retraction.relation_id = relation.relation_id
     and retraction.transaction_time <= p_known_at
    where relation.relation_id = p_relation_id
    group by relation.valid_to;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_resolve(p_scheme text, p_value text, p_valid_at date, p_known_at timestamp with time zone)
 RETURNS uuid
 LANGUAGE plpgsql
 STABLE
AS $function$
declare
    v_unique boolean;
    v_value text := staging.entity_alias_normalize(p_scheme, p_value);
    v_hits uuid[];
begin
    if p_valid_at is null or p_known_at is null then
        raise exception 'entity_resolve needs an explicit valid_at and known_at';
    end if;
    select is_unique into v_unique from staging.entity_alias_schemes where scheme = p_scheme;
    if v_unique is null then
        raise exception 'unknown alias scheme %', p_scheme;
    end if;
    if not v_unique then
        raise exception 'alias scheme % labels entities and cannot resolve one', p_scheme;
    end if;
    select array_agg(distinct staging.entity_survivor(alias.entity_id, p_known_at))
      into v_hits
    from staging.entity_aliases alias
    where alias.scheme = p_scheme
      and alias.value = v_value
      and alias.transaction_time <= p_known_at
      and alias.valid_from <= p_valid_at
      and coalesce(p_valid_at < staging.entity_alias_valid_to(alias.alias_id, p_known_at), true);
    if v_hits is null then
        return null;
    end if;
    if cardinality(v_hits) > 1 then
        raise exception 'alias %:% names % entities on % as known at %',
            p_scheme, v_value, cardinality(v_hits), p_valid_at, p_known_at;
    end if;
    return v_hits[1];
end;
$function$;
CREATE OR REPLACE FUNCTION staging.entity_survivor(p_entity_id uuid, p_known_at timestamp with time zone)
 RETURNS uuid
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
    with recursive chain(entity_id, depth) as (
        select p_entity_id, 0
        union all
        select edge.to_entity_id, chain.depth + 1
        from chain
        cross join lateral (
            select relation.to_entity_id
            from staging.entity_relations relation
            where relation.relation_type = 'superseded_by'
              and relation.from_entity_id = chain.entity_id
              and relation.transaction_time <= p_known_at
              and not exists (
                  select 1 from staging.entity_retractions retraction
                  where retraction.relation_id = relation.relation_id
                    and retraction.transaction_time <= p_known_at)
            order by relation.transaction_time desc, relation.relation_id desc
            limit 1
        ) edge
        where chain.depth < 32
    )
    select entity_id from chain order by depth desc limit 1;
$function$;
CREATE OR REPLACE FUNCTION staging.reject_cik_predecessor_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise check_violation using message = 'issuer_cik_predecessors is append-only registry history';
end;
$function$;
CREATE OR REPLACE FUNCTION staging.reject_entity_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'entity identity records are append-only; add an alias, relation or retraction instead';
end;
$function$;
CREATE OR REPLACE FUNCTION staging.reject_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'evidence records are append-only; append a new node/edge instead';
end;
$function$;
CREATE OR REPLACE FUNCTION staging.reject_pipeline_trigger_delete()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise check_violation using message = 'pipeline trigger requests are an append-only audit trail';
end;
$function$;
CREATE OR REPLACE FUNCTION staging.reject_point_in_time_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'point-in-time records are append-only; insert a new vintage';
end;
$function$;
CREATE OR REPLACE FUNCTION staging.require_entity_birth_alias()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if not exists (
        select 1 from staging.entity_aliases alias
        where alias.entity_id = new.entity_id
          and alias.scheme = new.birth_scheme
          and alias.value = new.birth_value
    ) then
        raise exception 'entity % was minted without its birth alias %:%',
            new.entity_id, new.birth_scheme, new.birth_value;
    end if;
    return null;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.validate_capture_observation_payload()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    expected_sha256 text;
begin
    select observation.normalized_payload_sha256
      into expected_sha256
      from staging.capture_normalized_observations observation
     where observation.observation_id = new.observation_id;
    if expected_sha256 is null
       or expected_sha256 <> new.normalized_payload_sha256
       or expected_sha256 <> raw.canonical_sha256(new.normalized_payload) then
        raise check_violation using message = 'capture observation payload does not match its normalized hash';
    end if;
    return new;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.validate_pipeline_trigger_update()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if old.consumed_at is not null then
        raise check_violation using message = 'pipeline trigger request already consumed';
    end if;
    if new.request_id <> old.request_id
       or new.job_name <> old.job_name
       or new.executed_at <> old.executed_at
       or new.requested_by <> old.requested_by
       or new.dedupe_key <> old.dedupe_key
       or new.requested_at <> old.requested_at
       or new.force_fetch <> old.force_fetch then
        raise check_violation using message = 'pipeline trigger request fields are immutable';
    end if;
    return new;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.validate_core_snapshot_member()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if new.observation_ids <> array(select unnest(new.observation_ids) order by 1)
       or cardinality(new.observation_ids) <> cardinality(array(select distinct unnest(new.observation_ids)))
       or raw.canonical_sha256(new.factor_input) <> new.member_sha256
       or new.factor_input->>'snapshot_id' <> new.snapshot_id
       or new.factor_input->>'instrument_id' <> new.instrument_id
       or new.factor_input->>'issuer_id' <> new.issuer_id
       or new.factor_input->>'listing_id' <> new.listing_id then
        raise check_violation using message = 'TOPT core snapshot member identity or payload drifted';
    end if;
    if exists (
        select 1 from unnest(new.observation_ids) observation_id
        left join staging.capture_normalized_observations observation using (observation_id)
        left join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id is null or payload.observation_id is null
    ) then
        raise check_violation using message = 'TOPT core snapshot member lacks durable normalized payload lineage';
    end if;
    return new;
end;
$function$;

CREATE OR REPLACE FUNCTION staging.validate_entity()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    v_kind staging.entity_kinds%rowtype;
begin
    select * into v_kind from staging.entity_kinds where kind = new.kind;
    if v_kind.is_abstract then
        raise exception 'entity kind % is abstract; mint a concrete kind', new.kind;
    end if;
    if new.birth_value is distinct from staging.entity_alias_normalize(new.birth_scheme, new.birth_value) then
        raise exception 'birth alias %:% is not in canonical form', new.birth_scheme, new.birth_value;
    end if;
    if new.entity_id <> staging.entity_uuid_v5(
            v_kind.uuid_namespace,
            staging.entity_birth_name(new.birth_scheme, new.birth_value, new.birth_generation)) then
        raise exception 'entity id % is not derived from its birth alias %:% (generation %)',
            new.entity_id, new.birth_scheme, new.birth_value, new.birth_generation;
    end if;
    return new;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.validate_entity_alias()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    v_scheme staging.entity_alias_schemes%rowtype;
    v_kind text;
    v_other record;
    v_as_of timestamptz;
    v_other_to date;
begin
    select * into v_scheme from staging.entity_alias_schemes where scheme = new.scheme;
    if new.value is distinct from staging.entity_alias_normalize(new.scheme, new.value) then
        raise exception 'alias %:% is not in canonical form (%)',
            new.scheme, new.value, staging.entity_alias_normalize(new.scheme, new.value);
    end if;
    if new.value !~ v_scheme.value_pattern then
        raise exception 'alias %:% does not match %', new.scheme, new.value, v_scheme.value_pattern;
    end if;
    select kind into v_kind from staging.entities where entity_id = new.entity_id;
    if not staging.entity_kind_is_a(v_kind, v_scheme.applies_to_kind) then
        raise exception 'scheme % identifies %, not %', new.scheme, v_scheme.applies_to_kind, v_kind;
    end if;
    if not v_scheme.is_unique then
        return new;
    end if;
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_aliases:' || new.scheme || ':' || new.value, 0));
    for v_other in
        select alias.alias_id, alias.entity_id, alias.valid_from, alias.transaction_time
        from staging.entity_aliases alias
        where alias.scheme = new.scheme and alias.value = new.value and alias.entity_id <> new.entity_id
    loop
        v_as_of := greatest(v_other.transaction_time, new.transaction_time);
        if staging.entity_survivor(v_other.entity_id, v_as_of)
           = staging.entity_survivor(new.entity_id, v_as_of) then
            continue;
        end if;
        v_other_to := staging.entity_alias_valid_to(v_other.alias_id, v_as_of);
        if v_other.valid_from < coalesce(new.valid_to, 'infinity'::date)
           and new.valid_from < coalesce(v_other_to, 'infinity'::date)
           and v_other.valid_from < coalesce(v_other_to, 'infinity'::date) then
            raise exception 'alias %:% already names entity % over an overlapping validity (alias %)',
                new.scheme, new.value, v_other.entity_id, v_other.alias_id;
        end if;
    end loop;
    return new;
end;
$function$;
CREATE OR REPLACE FUNCTION staging.validate_entity_relation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    v_type staging.entity_relation_types%rowtype;
    v_from_kind text;
    v_to_kind text;
begin
    select * into v_type from staging.entity_relation_types where relation_type = new.relation_type;
    select kind into v_from_kind from staging.entities where entity_id = new.from_entity_id;
    select kind into v_to_kind from staging.entities where entity_id = new.to_entity_id;
    if not staging.entity_kind_is_a(v_from_kind, v_type.domain_kind) then
        raise exception '% starts at a %, not %', new.relation_type, v_type.domain_kind, v_from_kind;
    end if;
    if not staging.entity_kind_is_a(v_to_kind, v_type.range_kind) then
        raise exception '% ends at a %, not %', new.relation_type, v_type.range_kind, v_to_kind;
    end if;
    if v_type.same_kind and v_from_kind <> v_to_kind then
        raise exception '% joins entities of one kind, not % and %', new.relation_type, v_from_kind, v_to_kind;
    end if;
    if new.relation_id <> staging.entity_relation_uuid(
            new.relation_type, new.from_entity_id, new.to_entity_id, new.valid_from,
            new.transaction_time, new.source, new.method) then
        raise exception 'relation id % is not derived from its claim', new.relation_id;
    end if;
    if new.relation_type = 'superseded_by' then
        perform pg_advisory_xact_lock(hashtextextended('staging.entity_relations:superseded_by', 0));
        if staging.entity_survivor(new.from_entity_id, 'infinity') <> new.from_entity_id then
            raise exception 'entity % is already superseded; merge its survivor instead', new.from_entity_id;
        end if;
        if staging.entity_survivor(new.to_entity_id, 'infinity') = new.from_entity_id then
            raise exception 'merging % into % would close a cycle', new.from_entity_id, new.to_entity_id;
        end if;
    end if;
    return new;
end;
$function$;

do $$ begin if to_regclass('staging.accepted_ruleset_head') is null then
execute $vc$create or replace view staging.accepted_ruleset_head as
 SELECT DISTINCT ON (accepted_rulesets.kind) accepted_rulesets.kind,
    accepted_rulesets.contract_id,
    accepted_rulesets.sequence,
    accepted_rulesets.note,
    accepted_rulesets.advanced_at
   FROM staging.accepted_rulesets
  ORDER BY accepted_rulesets.kind, accepted_rulesets.sequence DESC$vc$;
end if; end $$;
do $$ begin if to_regclass('staging.analyst_rating_events') is not null then drop table staging.analyst_rating_events; end if; end $$;
do $$ begin if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'staging' and c.relname = 'entity_backfill_plan' and md5(pg_get_viewdef(c.oid)) = '4fd2a143d605c541e78246cea4279cc5') then
execute $vc$create or replace view staging.entity_backfill_plan as
with usage as (
    select payload.normalized_payload->>'issuer_id' as issuer_id,
           payload.normalized_payload->>'instrument_id' as instrument_id,
           payload.normalized_payload->>'listing_id' as listing_id,
           coalesce(
               min(observation.knowable_at)
                   filter (where observation.semantic_type in ('listing-identity', 'universe-membership')),
               min(observation.knowable_at)) as known_at,
           (min(observation.valid_from) at time zone 'UTC')::date as valid_from,
           coalesce(
               max(observation.confidence)
                   filter (where observation.semantic_type in ('listing-identity', 'universe-membership')),
               max(observation.confidence)) as confidence,
           min(observation.observation_id) as raw_ref,
           'capture-observation' as source
    from staging.capture_observation_payloads payload
    join staging.capture_normalized_observations observation
      on observation.observation_id = payload.observation_id
    where jsonb_typeof(payload.normalized_payload->'issuer_id') = 'string'
      and jsonb_typeof(payload.normalized_payload->'instrument_id') = 'string'
      and jsonb_typeof(payload.normalized_payload->'listing_id') = 'string'
    group by 1, 2, 3
    union all
    select member->>0,
           member->>1,
           member->>2,
           min(head.recorded_at),
           min(case when head.payload->>'report_date' ~ '^\d{4}-\d{2}-\d{2}$'
                    then (head.payload->>'report_date')::date end),
           -- A published head is a frozen release projection; the capture grades the
           -- identity it derives from one as exact (release_derived_adapter), and so does this.
           1.0,
           min(head.contract_id),
           'universe-head'
    from staging.contract_objects head
    cross join lateral jsonb_array_elements(
        case when jsonb_typeof(head.payload->'instruments') = 'array'
             then head.payload->'instruments' else '[]'::jsonb end) member
    where head.contract_kind like 'universe-list:%'
      and head.payload->'instrument_tuple_fields'
          = '["issuer_id", "instrument_id", "listing_id", "ticker"]'::jsonb
      and jsonb_typeof(member) = 'array'
      and jsonb_typeof(member->0) = 'string'
      and jsonb_typeof(member->1) = 'string'
      and jsonb_typeof(member->2) = 'string'
    group by 1, 2, 3
), trio as (
    select issuer_id, instrument_id, listing_id,
           min(known_at) as known_at,
           coalesce(min(valid_from), (min(known_at) at time zone 'UTC')::date) as valid_from,
           max(confidence) as confidence,
           (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
           (array_agg(source order by known_at, raw_ref))[1] as source
    from usage
    group by 1, 2, 3
), legacy as (
    select role, legacy_id,
           min(known_at) as known_at,
           min(valid_from) as valid_from,
           (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
           (array_agg(source order by known_at, raw_ref))[1] as source,
           count(*) over (partition by legacy_id) as roles
    from (
        select 'issuer' as role, issuer_id as legacy_id, known_at, valid_from, raw_ref, source from trio
        union all
        select 'instrument', instrument_id, known_at, valid_from, raw_ref, source from trio
        union all
        select 'listing', listing_id, known_at, valid_from, raw_ref, source from trio
    ) used
    group by role, legacy_id
), parsed as (
    select legacy.*,
           case
               when role = 'issuer' and legacy_id ~ '^issuer:lei:[0-9A-Za-z]{20}$' then 'lei'
               when role = 'issuer' and legacy_id ~ '^issuer:cik:[0-9]{1,10}$' then 'cik'
               when role = 'instrument' and legacy_id ~ '^security:cusip:[0-9A-Za-z*@#]{9}$' then 'cusip'
               when role = 'instrument' and legacy_id ~ '^security:figi:[0-9A-Za-z]{12}$' then 'figi'
               when role = 'listing' and legacy_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$' then 'mic-ticker'
           end as scheme,
           case
               when role = 'issuer' and legacy_id ~ '^issuer:lei:[0-9A-Za-z]{20}$'
                   then upper(split_part(legacy_id, ':', 3))
               when role = 'issuer' and legacy_id ~ '^issuer:cik:[0-9]{1,10}$'
                   then lpad(split_part(legacy_id, ':', 3), 10, '0')
               when role = 'instrument' and legacy_id ~ '^security:cusip:[0-9A-Za-z*@#]{9}$'
                   then upper(split_part(legacy_id, ':', 3))
               when role = 'instrument' and legacy_id ~ '^security:figi:[0-9A-Za-z]{12}$'
                   then upper(split_part(legacy_id, ':', 3))
               when role = 'listing' and legacy_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
                   then upper(split_part(legacy_id, ':', 2)) || ':' || upper(split_part(legacy_id, ':', 3))
           end as value
    from legacy
), proof as (
    select line.id as line_id,
           upper(line.lei) as lei,
           upper(line.cusip) as cusip,
           upper(line.isin) as isin,
           line.report_period,
           cik_alias.value as cik,
           greatest(line.transaction_time, isin_alias.transaction_time, cik_alias.transaction_time, coalesce(rel.transaction_time, line.transaction_time)) as known_at,
           least(line.confidence, isin_alias.confidence, cik_alias.confidence, coalesce(rel.confidence, 1.0)) as confidence,
           line.raw_ref,
           jsonb_build_object(
               'fund_holding_fact_id', line.id,
               'nport_raw_ref', line.raw_ref,
               'isin_alias_id', isin_alias.alias_id,
               'isin_raw_ref', isin_alias.raw_ref,
               'issues_relation_id', rel.relation_id,
               'issuer_entity_id', coalesce(rel.from_entity_id, isin_alias.entity_id)) as evidence
    from staging.fund_holding_facts line
    cross join lateral (
        select ea.alias_id, ea.entity_id, ea.transaction_time, ea.confidence, ea.raw_ref
        from staging.entity_aliases ea
        where ea.scheme = 'isin' and ea.value = upper(line.isin)
        order by ea.transaction_time desc, ea.confidence desc, ea.alias_id desc
        limit 1
    ) isin_alias
    left join lateral (
        select r.relation_id, r.from_entity_id, r.transaction_time, r.confidence
        from staging.entity_relations r
        where r.to_entity_id = isin_alias.entity_id and r.relation_type = 'issues'
        order by r.transaction_time desc, r.relation_id desc
        limit 1
    ) rel on true
    cross join lateral (
        select ea.value, ea.transaction_time, ea.confidence
        from staging.entity_aliases ea
        where ea.entity_id = staging.entity_survivor(coalesce(rel.from_entity_id, isin_alias.entity_id), 'infinity'::timestamptz)
          and ea.scheme = 'cik'
        order by ea.transaction_time desc, ea.confidence desc, ea.alias_id desc
        limit 1
    ) cik_alias
    where line.lei ~ '^[0-9A-Za-z]{20}$'
      and line.isin ~ '^[0-9A-Za-z]{12}$'
), lei_link as (
    select lei,
           count(distinct cik) as ciks,
           min(cik) as cik,
           min(report_period) as valid_from,
           (array_agg(known_at order by known_at, line_id))[1] as known_at,
           (array_agg(confidence order by known_at, line_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, line_id))[1] as evidence
    from proof
    group by lei
), cik_leis as (
    select cik, count(distinct lei) as leis from proof group by cik
), cusip_isin as (
    select cusip,
           count(distinct isin) as isins,
           count(distinct cik) as ciks,
           min(isin) as isin,
           min(cik) as cik,
           min(report_period) as valid_from,
           (array_agg(known_at order by known_at, line_id))[1] as known_at,
           (array_agg(confidence order by known_at, line_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, line_id))[1] as evidence
    from proof
    where cusip ~ '^[0-9A-Z*@#]{9}$'
    group by cusip
), isin_cusips as (
    select isin, count(distinct cusip) as cusips from proof where cusip is not null group by isin
), issuer_component as (
    select parsed.legacy_id,
           case
               when parsed.roles > 1 then 'issuer/legacy=' || parsed.legacy_id
               when parsed.scheme = 'cik' then 'issuer/cik=' || parsed.value
               when parsed.scheme = 'lei' and lei_link.ciks = 1 and cik_leis.leis = 1
                   then 'issuer/cik=' || lei_link.cik
               when parsed.scheme = 'lei' then 'issuer/lei=' || parsed.value
               else 'issuer/legacy=' || parsed.legacy_id
           end as component,
           case
               when parsed.roles > 1 then 'conflict:id-used-in-several-roles'
               when parsed.scheme <> 'lei' or parsed.scheme is null then null
               when lei_link.lei is null then 'unlinked:no-crosswalk'
               when lei_link.ciks > 1 then 'unlinked:lei-proves-several-ciks'
               when cik_leis.leis > 1 then 'unlinked:cik-proven-for-several-leis'
           end as reason
    from parsed
    left join lei_link on parsed.scheme = 'lei' and lei_link.lei = parsed.value
    left join cik_leis on cik_leis.cik = lei_link.cik
    where parsed.role = 'issuer'
), pair as (
    select cusip_trio.instrument_id as cusip_id,
           figi_trio.instrument_id as figi_id,
           cusip_trio.listing_id,
           greatest(cusip_trio.known_at, figi_trio.known_at, cusip_isin.known_at) as known_at,
           least(cusip_trio.confidence, figi_trio.confidence, cusip_isin.confidence) as confidence,
           jsonb_build_object(
               'shared_listing_id', cusip_trio.listing_id,
               'cusip_trio_raw_ref', cusip_trio.raw_ref,
               'figi_trio_raw_ref', figi_trio.raw_ref,
               'issuer_component', cusip_issuer.component,
               'nport_proof', cusip_isin.evidence) as evidence,
           cusip_isin.raw_ref
    from trio cusip_trio
    join trio figi_trio
      on figi_trio.listing_id = cusip_trio.listing_id
    join parsed cusip_parsed
      on cusip_parsed.role = 'instrument' and cusip_parsed.legacy_id = cusip_trio.instrument_id
     and cusip_parsed.scheme = 'cusip'
    join parsed figi_parsed
      on figi_parsed.role = 'instrument' and figi_parsed.legacy_id = figi_trio.instrument_id
     and figi_parsed.scheme = 'figi'
    join issuer_component cusip_issuer on cusip_issuer.legacy_id = cusip_trio.issuer_id
    join issuer_component figi_issuer on figi_issuer.legacy_id = figi_trio.issuer_id
    join cusip_isin on cusip_isin.cusip = cusip_parsed.value
    where cusip_issuer.component = figi_issuer.component
      and cusip_issuer.component like 'issuer/cik=%'
      and cusip_isin.ciks = 1
      and 'issuer/cik=' || cusip_isin.cik = cusip_issuer.component
), cusip_link as (
    select cusip_id,
           count(distinct figi_id) as figis,
           min(figi_id) as figi_id,
           (array_agg(known_at order by known_at, figi_id))[1] as known_at,
           (array_agg(confidence order by known_at, figi_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, figi_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, figi_id))[1] as evidence
    from pair
    group by cusip_id
), figi_cusips as (
    select figi_id, count(distinct cusip_id) as cusips from pair group by figi_id
), instrument_component as (
    select parsed.legacy_id,
           case
               when parsed.roles > 1 then 'instrument/legacy=' || parsed.legacy_id
               when parsed.scheme = 'figi' then 'instrument/figi=' || parsed.value
               when parsed.scheme = 'cusip' and cusip_link.figis = 1 and figi_cusips.cusips = 1
                   then 'instrument/figi=' || figi_parsed.value
               when parsed.scheme = 'cusip' then 'instrument/cusip=' || parsed.value
               else 'instrument/legacy=' || parsed.legacy_id
           end as component,
           case
               when parsed.roles > 1 then 'conflict:id-used-in-several-roles'
               when parsed.scheme <> 'cusip' or parsed.scheme is null then null
               when cusip_link.cusip_id is null and not exists (
                   select 1
                   from trio mine
                   join trio other on other.listing_id = mine.listing_id
                   join parsed other_parsed
                     on other_parsed.role = 'instrument' and other_parsed.legacy_id = other.instrument_id
                    and other_parsed.scheme = 'figi'
                   where mine.instrument_id = parsed.legacy_id)
                   then 'unlinked:no-counterpart'
               when cusip_link.cusip_id is null then 'unlinked:no-crosswalk'
               when cusip_link.figis > 1 or figi_cusips.cusips > 1 then 'unlinked:ambiguous-counterpart'
           end as reason
    from parsed
    left join cusip_link on parsed.scheme = 'cusip' and cusip_link.cusip_id = parsed.legacy_id
    left join figi_cusips on figi_cusips.figi_id = cusip_link.figi_id
    left join parsed figi_parsed
      on figi_parsed.role = 'instrument' and figi_parsed.legacy_id = cusip_link.figi_id
    where parsed.role = 'instrument'
), listing_component as (
    select parsed.legacy_id,
           case
               when parsed.roles > 1 then 'listing/legacy=' || parsed.legacy_id
               when parsed.scheme = 'mic-ticker' then 'listing/mic-ticker=' || parsed.value
               else 'listing/legacy=' || parsed.legacy_id
           end as component,
           case when parsed.roles > 1 then 'conflict:id-used-in-several-roles' end as reason
    from parsed
    where parsed.role = 'listing'
), component as (
    select 'issuer' as kind, legacy_id, component, reason from issuer_component
    union all
    select 'instrument', legacy_id, component, reason from instrument_component
    union all
    select 'listing', legacy_id, component, reason from listing_component
), component_state as (
    select component.*,
           count(*) over (partition by component.kind, component.component) as legacy_ids,
           parsed.scheme,
           parsed.value,
           parsed.known_at,
           parsed.valid_from,
           parsed.raw_ref,
           parsed.source,
           case
               when component.reason like 'conflict:%' then component.reason
               when count(*) over (partition by component.kind, component.component) > 1 then 'linked'
               when component.reason is not null then component.reason
               when parsed.scheme = 'lei' then 'cik-proven:no-counterpart'
               else 'single-scheme'
           end as link_state
    from component
    join parsed on parsed.role = component.kind and parsed.legacy_id = component.legacy_id
), alias_claim as (
    -- the legacy id itself
    select kind, component, 'legacy-id' as scheme, legacy_id as value,
           valid_from, known_at, source, raw_ref,
           'asserted' as method, 1.0 as confidence,
           jsonb_build_object('used_by', source) as evidence,
           legacy_id, link_state
    from component_state
    union all
    -- the typed id the legacy id spells out
    select kind, component, scheme, value,
           valid_from, known_at, source, raw_ref,
           'parsed', 1.0,
           jsonb_build_object('parsed_from', legacy_id),
           legacy_id, link_state
    from component_state
    where scheme is not null
    union all
    -- the CIK an LEI-keyed issuer was proven to hold
    select state.kind, state.component, 'cik', lei_link.cik,
           lei_link.valid_from, lei_link.known_at, 'nport+kg', lei_link.raw_ref,
           'crosswalk', lei_link.confidence,
           lei_link.evidence || jsonb_build_object('lei', state.value),
           state.legacy_id, state.link_state
    from component_state state
    join lei_link on lei_link.lei = state.value
    where state.kind = 'issuer' and state.scheme = 'lei'
      and state.component = 'issuer/cik=' || lei_link.cik
    union all
    -- the ISIN on the N-PORT line of a CUSIP-keyed instrument
    select state.kind, state.component, 'isin', cusip_isin.isin,
           cusip_isin.valid_from, cusip_isin.known_at, 'nport+kg', cusip_isin.raw_ref,
           'crosswalk', cusip_isin.confidence,
           cusip_isin.evidence || jsonb_build_object('cusip', state.value),
           state.legacy_id, state.link_state
    from component_state state
    join cusip_isin on cusip_isin.cusip = state.value
    join isin_cusips on isin_cusips.isin = cusip_isin.isin
    join trio on trio.instrument_id = state.legacy_id
    join issuer_component issuer on issuer.legacy_id = trio.issuer_id
    where state.kind = 'instrument' and state.scheme = 'cusip'
      and cusip_isin.isins = 1 and cusip_isin.ciks = 1 and isin_cusips.cusips = 1
      and (substring(cusip_isin.isin from 1 for 2) not in ('US', 'CA')
           or substring(cusip_isin.isin from 3 for 9) = cusip_isin.cusip)
      and issuer.component = 'issuer/cik=' || cusip_isin.cik
    group by state.kind, state.component, cusip_isin.isin, cusip_isin.valid_from, cusip_isin.known_at,
             cusip_isin.raw_ref, cusip_isin.confidence, cusip_isin.evidence, state.value,
             state.legacy_id, state.link_state
    union all
    -- the evidence that joined a CUSIP-keyed and a FIGI-keyed instrument
    select state.kind, state.component, 'cusip', state.value,
           state.valid_from, cusip_link.known_at, 'shared-listing+nport+kg', cusip_link.raw_ref,
           'crosswalk', cusip_link.confidence,
           cusip_link.evidence || jsonb_build_object('figi_legacy_id', cusip_link.figi_id),
           state.legacy_id, state.link_state
    from component_state state
    join cusip_link on cusip_link.cusip_id = state.legacy_id
    where state.kind = 'instrument' and state.scheme = 'cusip'
      and state.component like 'instrument/figi=%'
), relation_use as (
    select 'issues' as relation_type,
           'issuer' as from_kind, issuer.component as from_component,
           'instrument' as to_kind, instrument.component as to_component,
           trio.known_at, trio.valid_from, trio.confidence, trio.raw_ref, trio.source,
           issuer.reason like 'conflict:%' or instrument.reason like 'conflict:%' as blocked
    from trio
    join issuer_component issuer on issuer.legacy_id = trio.issuer_id
    join instrument_component instrument on instrument.legacy_id = trio.instrument_id
    union all
    select 'listed_as',
           'instrument', instrument.component,
           'listing', listing.component,
           trio.known_at, trio.valid_from, trio.confidence, trio.raw_ref, trio.source,
           instrument.reason like 'conflict:%' or listing.reason like 'conflict:%'
    from trio
    join instrument_component instrument on instrument.legacy_id = trio.instrument_id
    join listing_component listing on listing.legacy_id = trio.listing_id
), relation_claim as (
    select relation_type, from_kind, from_component, to_kind, to_component,
           min(known_at) as known_at,
           min(valid_from) as valid_from,
           max(confidence) as confidence,
           (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
           (array_agg(source order by known_at, raw_ref))[1] as source,
           bool_or(coalesce(blocked, false)) as blocked
    from relation_use
    group by 1, 2, 3, 4, 5
), relation_state as (
    select relation_claim.*,
           -- An instrument has one issuer and a listing one instrument: two different
           -- components on the "one" side means the ids were not joined, and neither edge is
           -- written until they are.
           count(*) over (partition by relation_type, to_kind, to_component) as claimants
    from relation_claim
)
select 'alias' as claim,
       kind,
       component,
       scheme,
       value,
       null::text as relation_type,
       null::text as to_kind,
       null::text as to_component,
       valid_from,
       known_at as transaction_time,
       source,
       raw_ref,
       method,
       confidence,
       evidence,
       legacy_id,
       link_state,
       case when link_state like 'conflict:%' then link_state else 'planned' end as status
from alias_claim
union all
select 'relation',
       from_kind,
       from_component,
       null,
       null,
       relation_type,
       to_kind,
       to_component,
       valid_from,
       known_at,
       source,
       raw_ref,
       'derived',
       confidence,
       jsonb_build_object('co_occurring_in', raw_ref),
       null,
       null,
       case
           when blocked then 'conflict:endpoint-in-conflict'
           when claimants > 1 and relation_type = 'issues' then 'conflict:instrument-claimed-by-several-issuers'
           when claimants > 1 then 'conflict:listing-claimed-by-several-instruments'
           else 'planned'
       end
from relation_state$vc$;
end if; end $$;

do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.accepted_rulesets'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '30ab83bc19361d65b937ab567f44c01e') then drop trigger if exists reject_mutation on staging.accepted_rulesets; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.accepted_rulesets FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.capture_normalized_observations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '6f51a39c2f4039549a3690d013a02642') then drop trigger if exists reject_mutation on staging.capture_normalized_observations; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.capture_normalized_observations FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.capture_observation_obligations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '93403becc156e47f1b723c6a5098884e') then drop trigger if exists reject_mutation on staging.capture_observation_obligations; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.capture_observation_obligations FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.capture_observation_payloads'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'f5ad787327c1627169c17aabc17a3018') then drop trigger if exists reject_mutation on staging.capture_observation_payloads; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.capture_observation_payloads FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.capture_observation_payloads'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'f4360aa6a4846b48ab3649dcebbebb0f') then drop trigger if exists validate_payload on staging.capture_observation_payloads; CREATE TRIGGER validate_payload BEFORE INSERT ON staging.capture_observation_payloads FOR EACH ROW EXECUTE FUNCTION staging.validate_capture_observation_payload(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.contract_objects'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'da0bd3ac58b213b7c1bf169079bc1b6f') then drop trigger if exists trg_contract_objects_append_only on staging.contract_objects; CREATE TRIGGER trg_contract_objects_append_only BEFORE DELETE OR UPDATE ON staging.contract_objects FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entities'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'ccf1e0ba4d099f14c49a54b7d873a066') then drop trigger if exists reject_mutation on staging.entities; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entities FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entities'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '775d3302e0c9f2363cb743bce9e04533') then drop trigger if exists require_birth_alias on staging.entities; CREATE CONSTRAINT TRIGGER require_birth_alias AFTER INSERT ON staging.entities DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION staging.require_entity_birth_alias(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entities'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'facd84077cd660b9c72c47bd70d4b2c2') then drop trigger if exists validate_entity on staging.entities; CREATE TRIGGER validate_entity BEFORE INSERT ON staging.entities FOR EACH ROW EXECUTE FUNCTION staging.validate_entity(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_alias_schemes'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'c09054f9fdec46285f80d02fa3e8a1f4') then drop trigger if exists reject_mutation on staging.entity_alias_schemes; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_alias_schemes FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_aliases'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '13167ee46b2319bbb7f1c823da437fa3') then drop trigger if exists reject_mutation on staging.entity_aliases; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_aliases FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_aliases'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '7d9ae47cdefd00a1c3d377d0953043ef') then drop trigger if exists validate_entity_alias on staging.entity_aliases; CREATE TRIGGER validate_entity_alias BEFORE INSERT ON staging.entity_aliases FOR EACH ROW EXECUTE FUNCTION staging.validate_entity_alias(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_kinds'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'c4568c358fe1e3687374d70d3a67416a') then drop trigger if exists reject_mutation on staging.entity_kinds; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_kinds FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_relation_types'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'b881606e1e90cfccf109953f030dfdc4') then drop trigger if exists reject_mutation on staging.entity_relation_types; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_relation_types FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_relations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '480efda9c2bbc83f89b944b35e8f1cbc') then drop trigger if exists reject_mutation on staging.entity_relations; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_relations FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_relations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '6cf6fd7031a58ae7de4b6edf00682f97') then drop trigger if exists validate_entity_relation on staging.entity_relations; CREATE TRIGGER validate_entity_relation BEFORE INSERT ON staging.entity_relations FOR EACH ROW EXECUTE FUNCTION staging.validate_entity_relation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.entity_retractions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'dcc16bbce4f61d26b52418a2ad476680') then drop trigger if exists reject_mutation on staging.entity_retractions; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.entity_retractions FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.etf_constituent_facts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'f17a3c45d45a71fb816863801d40f3e0') then drop trigger if exists reject_mutation on staging.etf_constituent_facts; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.etf_constituent_facts FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.evidence_edges'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '4d6583ad9124a5267924b8caf08761d3') then drop trigger if exists reject_mutation on staging.evidence_edges; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.evidence_edges FOR EACH ROW EXECUTE FUNCTION staging.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.evidence_nodes'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '787e75fc90245b834df2050466046f9d') then drop trigger if exists reject_mutation on staging.evidence_nodes; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.evidence_nodes FOR EACH ROW EXECUTE FUNCTION staging.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.fund_holding_facts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '60b314eeff5d0a2d088b13bb79107053') then drop trigger if exists trg_fund_holding_facts_append_only on staging.fund_holding_facts; CREATE TRIGGER trg_fund_holding_facts_append_only BEFORE DELETE OR UPDATE ON staging.fund_holding_facts FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.issuer_cik_predecessors'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '829de5f44febe7eb69d98026f74e98b0') then drop trigger if exists reject_mutation on staging.issuer_cik_predecessors; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.issuer_cik_predecessors FOR EACH ROW EXECUTE FUNCTION staging.reject_cik_predecessor_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.issuer_headcount_facts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '4e060aa7687460b6e7a3a7a68975faf9') then drop trigger if exists reject_mutation on staging.issuer_headcount_facts; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.issuer_headcount_facts FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.issuer_segment_revenue_facts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '6f6441f8cbf8b43ad6db6cca44cab3ed') then drop trigger if exists reject_mutation on staging.issuer_segment_revenue_facts; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.issuer_segment_revenue_facts FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.market_prices_daily'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '1afdcd030642604e375467d5deafaa36') then drop trigger if exists trg_market_prices_daily_append_only on staging.market_prices_daily; CREATE TRIGGER trg_market_prices_daily_append_only BEFORE DELETE OR UPDATE ON staging.market_prices_daily FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.model_invocations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'f4eed3ab505235da7ad42f852a85b296') then drop trigger if exists reject_mutation on staging.model_invocations; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.model_invocations FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.pipeline_trigger_requests'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '60b2cbed702e09257811e325bd3d8422') then drop trigger if exists reject_trigger_delete on staging.pipeline_trigger_requests; CREATE TRIGGER reject_trigger_delete BEFORE DELETE ON staging.pipeline_trigger_requests FOR EACH ROW EXECUTE FUNCTION staging.reject_pipeline_trigger_delete(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.pipeline_trigger_requests'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'ccb40a047cd2d232acdbb00c9659605b') then drop trigger if exists validate_trigger_update on staging.pipeline_trigger_requests; CREATE TRIGGER validate_trigger_update BEFORE UPDATE ON staging.pipeline_trigger_requests FOR EACH ROW EXECUTE FUNCTION staging.validate_pipeline_trigger_update(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.core_snapshot_members'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '56604115308729397b03ef37195b8e35') then drop trigger if exists reject_mutation on staging.core_snapshot_members; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.core_snapshot_members FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.core_snapshot_members'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '1824f0b6a6a1d9b451608875e265ddb7') then drop trigger if exists validate_member on staging.core_snapshot_members; CREATE TRIGGER validate_member BEFORE INSERT ON staging.core_snapshot_members FOR EACH ROW EXECUTE FUNCTION staging.validate_core_snapshot_member(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'staging.core_snapshots'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '7a1b7a74986466bcc6ce6aeecdbfa49f') then drop trigger if exists reject_mutation on staging.core_snapshots; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.core_snapshots FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;

do $$ begin if to_regclass('staging.ix_api_call_ledger_called_at') is null then CREATE INDEX ix_api_call_ledger_called_at ON staging.api_call_ledger USING btree (called_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.ix_api_call_ledger_fetch_key') is null then CREATE INDEX ix_api_call_ledger_fetch_key ON staging.api_call_ledger USING btree (called_at, fetch_key) WHERE (fetch_key IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.ix_api_call_ledger_payload_sha256') is null then CREATE INDEX ix_api_call_ledger_payload_sha256 ON staging.api_call_ledger USING btree (payload_sha256) WHERE (payload_sha256 IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.ix_api_call_ledger_source_called_at') is null then CREATE INDEX ix_api_call_ledger_source_called_at ON staging.api_call_ledger USING btree (source, called_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_contract_objects_kind_recorded') is null then CREATE INDEX idx_contract_objects_kind_recorded ON staging.contract_objects USING btree (contract_kind, recorded_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_aliases_entity') is null then CREATE INDEX idx_entity_aliases_entity ON staging.entity_aliases USING btree (entity_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_relations_from') is null then CREATE INDEX idx_entity_relations_from ON staging.entity_relations USING btree (from_entity_id, relation_type); end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_relations_to') is null then CREATE INDEX idx_entity_relations_to ON staging.entity_relations USING btree (to_entity_id, relation_type); end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_retractions_alias') is null then CREATE INDEX idx_entity_retractions_alias ON staging.entity_retractions USING btree (alias_id) WHERE (alias_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_retractions_relation') is null then CREATE INDEX idx_entity_retractions_relation ON staging.entity_retractions USING btree (relation_id) WHERE (relation_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.etf_constituent_facts_lookup') is null then CREATE INDEX etf_constituent_facts_lookup ON staging.etf_constituent_facts USING btree (etf_symbol, as_of DESC, ticker); end if; end $$;
do $$ begin if to_regclass('staging.idx_fund_holding_facts_asof') is null then CREATE INDEX idx_fund_holding_facts_asof ON staging.fund_holding_facts USING btree (fund_id, report_period, transaction_time DESC); end if; end $$;
do $$ begin if to_regclass('staging.uq_fund_holding_line_vintage') is null then CREATE UNIQUE INDEX uq_fund_holding_line_vintage ON staging.fund_holding_facts USING btree (fund_id, holding_name, report_period, transaction_time, COALESCE(isin, ''::text), COALESCE(cusip, ''::text)); end if; end $$;
do $$ begin if to_regclass('staging.idx_issuer_headcount_pit') is null then CREATE INDEX idx_issuer_headcount_pit ON staging.issuer_headcount_facts USING btree (cik, knowable_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.ix_issuer_segment_revenue_facts_pit') is null then CREATE INDEX ix_issuer_segment_revenue_facts_pit ON staging.issuer_segment_revenue_facts USING btree (cik, knowable_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_market_prices_daily_symbol_date') is null then CREATE INDEX idx_market_prices_daily_symbol_date ON staging.market_prices_daily USING btree (symbol, trading_date, transaction_time DESC, recorded_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.ix_model_invocations_subject') is null then CREATE INDEX ix_model_invocations_subject ON staging.model_invocations USING btree (subject_cik, accession, prompt_sha256, model, id DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_strategy_backtest_inputs_asof') is null then CREATE INDEX idx_strategy_backtest_inputs_asof ON staging.strategy_backtest_inputs USING btree (cutoff_at, issuer_id, input_key, recorded_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_strategy_backtest_inputs_period') is null then CREATE INDEX idx_strategy_backtest_inputs_period ON staging.strategy_backtest_inputs USING btree (cutoff_at, issuer_id, input_key, fiscal_period, recorded_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.strategy_backtest_inputs_identity') is null then CREATE UNIQUE INDEX strategy_backtest_inputs_identity ON staging.strategy_backtest_inputs USING btree (issuer_id, cutoff_at, input_key, COALESCE(fiscal_period, ''::text), recorded_at); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_obligation') is null then CREATE INDEX idx_capture_obs_obligation ON staging.capture_normalized_observations USING btree (capture_obligation_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_vintage') is null then CREATE INDEX idx_capture_obs_vintage ON staging.capture_normalized_observations USING btree (source_vintage_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_supersedes') is null then CREATE INDEX idx_capture_obs_supersedes ON staging.capture_normalized_observations USING btree (supersedes_observation_id) WHERE (supersedes_observation_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_subject_pit') is null then CREATE INDEX idx_capture_obs_subject_pit ON staging.capture_normalized_observations USING btree (subject_kind, subject_id, semantic_type, knowable_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_obligations_obs') is null then CREATE INDEX idx_capture_obs_obligations_obs ON staging.capture_observation_obligations USING btree (observation_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_etf_constituent_facts_raw_fetch') is null then CREATE INDEX idx_etf_constituent_facts_raw_fetch ON staging.etf_constituent_facts USING btree (raw_fetch_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_landed_fetch_keys_raw_fetch') is null then CREATE INDEX idx_landed_fetch_keys_raw_fetch ON staging.landed_fetch_keys USING btree (raw_fetch_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_edges_from_id') is null then CREATE INDEX idx_evidence_edges_from_id ON staging.evidence_edges USING btree (from_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_edges_to_id') is null then CREATE INDEX idx_evidence_edges_to_id ON staging.evidence_edges USING btree (to_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_nodes_supersedes') is null then CREATE INDEX idx_evidence_nodes_supersedes ON staging.evidence_nodes USING btree (supersedes_node_id) WHERE (supersedes_node_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.idx_fund_holding_facts_holding') is null then CREATE INDEX idx_fund_holding_facts_holding ON staging.fund_holding_facts USING btree (holding_id) WHERE (holding_id IS NOT NULL); end if; end $$;

insert into staging.entity_kinds (kind, parent_kind, is_abstract, uuid_namespace, description)
select kind, parent_kind, is_abstract,
       case when not is_abstract then staging.entity_uuid_namespace('kind:' || kind) end,
       description
from (values
    ('entity', null, true, 'Root of the ontology. Nothing is minted as a bare entity.'),
    ('agent', 'entity', true, 'Something that acts: organizations and people.'),
    ('organization', 'agent', true, 'A legal or operating organization.'),
    ('issuer', 'organization', false,
     'An organization that issues financial instruments (an operating company).'),
    ('fund', 'issuer', false,
     'A registered fund series (for example an ETF): issues its own shares and holds instruments.'),
    ('person', 'agent', false, 'A natural person (analyst, executive).'),
    ('instrument', 'entity', false,
     'A financial instrument at share-class level (what CUSIP, ISIN and a share-class FIGI name).'),
    ('listing', 'entity', false,
     'One instrument''s trading line on one venue (what MIC plus symbol names).'),
    ('universe', 'entity', false, 'A governed membership list (TOPT, QQQ, canary). No writer yet.'),
    ('segment', 'entity', false, 'A reportable business segment of an issuer. No writer yet.'),
    ('product', 'entity', false, 'A product or service line. No writer yet.')
) as seed (kind, parent_kind, is_abstract, description)
where not exists (select 1 from staging.entity_kinds as present where present.kind = seed.kind)
on conflict (kind) do nothing;
insert into staging.entity_alias_schemes
    (scheme, applies_to_kind, is_unique, value_reusable, value_pattern, authority, description)
select seed.*
from (values
    ('legacy-id', 'entity', true, false, '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$', 'TrueAlpha',
     'A pre-#877 text id (issuer:lei:…, security:figi:…, listing:xnas:aapl). Case-sensitive, stored verbatim.'),
    ('lei', 'organization', true, false, '^[0-9A-Z]{18}[0-9]{2}$', 'GLEIF (ISO 17442)',
     'Legal Entity Identifier.'),
    ('cik', 'organization', true, false, '^[0-9]{10}$', 'SEC EDGAR',
     'Central Index Key, zero-padded to ten digits.'),
    ('cusip', 'instrument', true, true, '^[0-9A-Z*@#]{8}[0-9]$', 'CUSIP Global Services',
     'Nine-character CUSIP as printed in SEC filings. A retired number can be reassigned.'),
    ('isin', 'instrument', true, true, '^[A-Z]{2}[0-9A-Z]{9}[0-9]$', 'ISO 6166',
     'International Securities Identification Number. US and CA ISINs embed the CUSIP, so they inherit its reuse.'),
    ('figi', 'instrument', true, false, '^[B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9]$', 'OpenFIGI (OMG FIGI)',
     'Share-class FIGI where the vendor returns one. FIGI values are never reused at any level.'),
    ('mic-ticker', 'listing', true, true, '^[A-Z0-9]{4}:[A-Z0-9][A-Z0-9.\-]*$', 'ISO 10383 MIC + venue symbol',
     'Venue MIC and trading symbol, for example XNAS:AAPL. Symbols are reassigned over time.'),
    ('sec-series', 'fund', true, false, '^S[0-9]{9}$', 'SEC EDGAR',
     'Registered fund series id (S000…). No writer yet.'),
    ('moomoo-code', 'listing', true, true, '^[A-Z]{2}\.[A-Z0-9][A-Z0-9.\-]*$', 'moomoo OpenAPI',
     'Market-prefixed quote code, for example US.AAPL. No writer yet.'),
    ('ticker', 'entity', false, true, '^[A-Z0-9][A-Z0-9.\-]*$', 'the asserting source',
     'Unqualified trading symbol asserted when venue MIC is unknown.'),
    ('name', 'entity', false, true, '^\S(.*\S)?$', 'the asserting source',
     'A display label. Not unique; never resolves an entity.')
) as seed (scheme, applies_to_kind, is_unique, value_reusable, value_pattern, authority, description)
where not exists (select 1 from staging.entity_alias_schemes as present where present.scheme = seed.scheme)
on conflict (scheme) do nothing;
insert into staging.entity_relation_types
    (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description, uuid_namespace)
select seed.*, staging.entity_uuid_namespace('relation:' || seed.relation_type)
from (values
    ('issues', 'issuer', 'instrument', false, false, 'issued_by',
     'The issuer of an instrument.'),
    ('listed_as', 'instrument', 'listing', false, false, 'listing_of',
     'An instrument trades as this listing. A listing belongs to one instrument at a time.'),
    ('holds', 'fund', 'instrument', false, false, 'held_by',
     'A fund holds an instrument. Weights stay in staging.fund_holding_facts.'),
    ('member_of', 'entity', 'universe', false, false, 'has_member',
     'Membership of a governed universe over a validity interval. No writer yet.'),
    ('subsidiary_of', 'organization', 'organization', false, false, 'parent_of',
     'Corporate control. No writer yet.'),
    ('successor_of', 'organization', 'organization', false, false, 'predecessor_of',
     'A reorganized successor of an organization (the #496 holdco case). No writer yet.'),
    ('segment_of', 'segment', 'issuer', false, false, 'has_segment',
     'A reportable segment of an issuer. No writer yet.'),
    ('produces', 'organization', 'product', false, false, 'produced_by',
     'An organization offers a product. No writer yet.'),
    ('supplies_to', 'organization', 'organization', false, false, 'customer_of',
     'A disclosed supplier relationship.'),
    ('covers', 'person', 'issuer', false, false, 'covered_by',
     'An analyst covers an issuer. No writer yet.'),
    ('same_as', 'entity', 'entity', true, true, 'same_as',
     'Evidence that two entities are one real-world thing.'),
    ('superseded_by', 'entity', 'entity', true, true, 'supersedes',
     'A merge: the source entity''s identity continues as the target. The resolver follows it.'),
    ('split_into', 'entity', 'entity', true, true, 'split_from',
     'A split: from valid_from on, part of the source entity continues as the target.'),
    ('primary_listing', 'issuer', 'listing', false, false, 'primary_listing_of',
     'The listing that the engines use for an issuer with several listings. The classification file names it.')
) as seed (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description)
where not exists (
    select 1 from staging.entity_relation_types as present where present.relation_type = seed.relation_type
)
on conflict (relation_type) do nothing;
insert into staging.issuer_cik_predecessors (issuer_id, predecessor_cik, reason, approved_by)
select
    'issuer:lei:J3WHBG0MTS7O8ZVMDC91',
    34088,
    '2026 ExxonMobil holdco reorganization: index CIK 2115436 publishes no us-gaap taxonomy yet; no machine-readable predecessor pointer exists in SEC metadata',
    'zitian (owner decision 2026-07-29, truealpha#496)'
where not exists (
    select 1 from staging.issuer_cik_predecessors
    where issuer_id = 'issuer:lei:J3WHBG0MTS7O8ZVMDC91'
)
on conflict (issuer_id) do nothing;

do $$
begin
    if to_regclass('staging.capture_entity_refs') is not null then
        drop table staging.capture_entity_refs;
    end if;
    if to_regclass('staging.market_prices_monthly') is not null then
        drop table staging.market_prices_monthly;
    end if;
    if to_regclass('staging.universe_mask') is not null then
        drop table staging.universe_mask;
    end if;
    if to_regclass('staging.idx_contract_objects_quality_review_run') is not null then
        drop index staging.idx_contract_objects_quality_review_run;
    end if;
    if to_regclass('staging.idx_contract_objects_usage_audit_run') is not null then
        drop index staging.idx_contract_objects_usage_audit_run;
    end if;
end $$;
