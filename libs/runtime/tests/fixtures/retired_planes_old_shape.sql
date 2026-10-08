-- Test fixture for #1061: the 15 retired tables and their 13 functions.
-- It holds the shape that the migration chain of commit 4769e93 created.
-- Source: pg_dump --schema-only and pg_get_functiondef on a database that ran that chain.
-- Apply it to a database that ran the current chain.
-- The result has the shape of a deployed Staging or Production database before the retire migration.
-- libs/runtime/tests/test_migration_retire_empty_planes.py is the only user.

SET check_function_bodies = false;

CREATE OR REPLACE FUNCTION staging.validate_normalized_raw_lineage()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    fetch_id bigint;
    fetch_sha256 text;
    fetch_recorded_at timestamptz;
begin
    fetch_id := split_part(new.raw_ref, ':', 2)::bigint;
    select payload_sha256, recorded_at
    into fetch_sha256, fetch_recorded_at
    from raw.fetches
    where id = fetch_id;

    if not found then
        raise exception 'normalized raw_ref % does not exist', new.raw_ref
            using errcode = '23503';
    end if;
    if fetch_sha256 <> new.raw_object_sha256 then
        raise exception 'normalized raw checksum does not match %', new.raw_ref
            using errcode = '23514';
    end if;
    if new.recorded_at < fetch_recorded_at then
        raise exception 'normalized record cannot predate its raw landing'
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION staging.validate_filing_document_projection()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    normalized staging.normalized_records%rowtype;
begin
    select *
    into normalized
    from staging.normalized_records
    where normalized_record_id = new.normalized_record_id;

    if not found then
        raise exception 'filing projection has no normalized record %', new.normalized_record_id
            using errcode = '23503';
    end if;
    if normalized.semantic_type_id <> 'semantic.filing-document'
       or normalized.subject_kind <> 'issuer'
       or normalized.subject_id <> new.issuer_id
       or normalized.document_id <> new.document_id
       or normalized.valid_time <> new.valid_time
       or normalized.transaction_time <> new.transaction_time
       or normalized.recorded_at <> new.recorded_at
       or normalized.confidence <> new.confidence
       or normalized.raw_ref <> new.raw_ref
       or normalized.raw_object_sha256 <> new.content_sha256 then
        raise exception 'filing projection does not match its normalized record %', new.normalized_record_id
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION staging.validate_normalized_restatement()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    predecessor staging.normalized_records%rowtype;
begin
    if new.supersedes_record_id is null then
        return new;
    end if;

    select *
    into predecessor
    from staging.normalized_records
    where normalized_record_id = new.supersedes_record_id;

    if not found then
        raise exception 'superseded normalized record % does not exist', new.supersedes_record_id
            using errcode = '23503';
    end if;
    if predecessor.semantic_type_id <> new.semantic_type_id
       or predecessor.semantic_type_version <> new.semantic_type_version
       or predecessor.subject_kind <> new.subject_kind
       or predecessor.subject_id <> new.subject_id
       or predecessor.valid_time <> new.valid_time
       or predecessor.source_registry_entry_id <> new.source_registry_entry_id
       or predecessor.source_registry_entry_sha256 <> new.source_registry_entry_sha256 then
        raise exception 'restatement must retain its registry-bound semantic coordinate'
            using errcode = '23514';
    end if;
    if new.transaction_time <= predecessor.transaction_time then
        raise exception 'restatement transaction time must be strictly later'
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION staging.validate_headcount_invocation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    document staging.normalized_records%rowtype;
begin
    select *
    into document
    from staging.normalized_records
    where normalized_record_id = new.source_document_record_id;

    if not found then
        raise exception 'headcount invocation document % does not exist', new.source_document_record_id
            using errcode = '23503';
    end if;
    if document.semantic_type_id <> 'semantic.filing-document'
       or document.subject_kind <> 'issuer'
       or document.document_id <> new.document_id
       or document.raw_object_sha256 <> new.document_sha256
       or document.raw_ref <> new.raw_ref then
        raise exception 'headcount invocation does not match its D1 filing document'
            using errcode = '23514';
    end if;
    if new.started_at < document.recorded_at then
        raise exception 'headcount invocation cannot predate the recorded D1 filing'
            using errcode = '23514';
    end if;
    if new.invocation->>'extraction_invocation_id' <> new.extraction_invocation_id
       or new.invocation->>'content_sha256' <> new.content_sha256
       or new.invocation->>'model_revision_id' <> new.model_revision_id
       or new.invocation->>'model_revision_sha256' <> new.model_revision_sha256
       or new.invocation->>'extraction_template_id' <> new.extraction_template_id
       or new.invocation->>'extraction_template_sha256' <> new.extraction_template_sha256
       or new.invocation->>'input_sha256' <> new.input_sha256
       or new.invocation->>'response_sha256' <> new.response_sha256
       or new.invocation->>'semantic_payload_sha256' <> new.semantic_payload_sha256 then
        raise exception 'headcount invocation columns do not match the frozen invocation'
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION staging.validate_headcount_projection()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    normalized staging.normalized_records%rowtype;
    invocation staging.headcount_extraction_invocations%rowtype;
begin
    select *
    into normalized
    from staging.normalized_records
    where normalized_record_id = new.normalized_record_id;
    select *
    into invocation
    from staging.headcount_extraction_invocations
    where extraction_invocation_id = new.extraction_invocation_id;

    if normalized.normalized_record_id is null or invocation.extraction_invocation_id is null then
        raise exception 'headcount projection is missing normalized or invocation lineage'
            using errcode = '23503';
    end if;
    if normalized.semantic_type_id <> 'semantic.employee-headcount'
       or normalized.subject_kind <> 'issuer'
       or normalized.subject_id <> new.issuer_id
       or not (normalized.valid_time @> new.valid_period_end)
       or normalized.transaction_time <> new.transaction_time
       or normalized.recorded_at <> new.recorded_at
       or normalized.confidence <> new.confidence
       or normalized.raw_ref <> new.raw_ref
       or normalized.document_id <> invocation.document_id
       or normalized.raw_object_sha256 <> invocation.document_sha256
       or normalized.record_ref <> new.record_ref
       or normalized.payload <> new.payload then
        raise exception 'headcount projection does not match its normalized record'
            using errcode = '23514';
    end if;
    if normalized.record_ref #>> '{draft,extraction_invocation_id}' <> new.extraction_invocation_id
       or normalized.record_ref #>> '{draft,extraction_invocation_sha256}' <> invocation.content_sha256
       or normalized.payload_sha256 <> invocation.semantic_payload_sha256
       or new.payload->>'content_sha256' <> normalized.payload_sha256
       or new.payload->>'availability' <> new.availability
       or (new.payload->>'valid_period_end')::date <> new.valid_period_end
       or (new.payload->>'confidence')::numeric <> new.confidence
       or new.payload->>'review_status' <> new.review_status then
        raise exception 'headcount projection does not match its extraction payload'
            using errcode = '23514';
    end if;
    if new.availability = 'available' and (
        (new.payload #>> '{selected,value}')::bigint <> new.value
        or new.payload #>> '{selected,unit}' <> new.unit
        or new.payload #>> '{selected,scope}' <> new.scope
        or jsonb_array_length(new.payload #> '{selected,evidence_spans}') = 0
    ) then
        raise exception 'available headcount projection lacks its selected total evidence'
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION staging.validate_mvp_projection()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    normalized staging.normalized_records%rowtype;
    expected_type text := tg_argv[0];
begin
    select * into normalized
    from staging.normalized_records
    where normalized_record_id = new.normalized_record_id;

    if not found then
        raise exception 'MVP projection has no normalized record %', new.normalized_record_id
            using errcode = '23503';
    end if;
    if normalized.semantic_type_id <> expected_type
       or normalized.subject_kind <> new.subject_kind
       or normalized.subject_id <> new.subject_id
       or normalized.valid_time <> new.valid_time
       or normalized.transaction_time <> new.transaction_time
       or normalized.recorded_at <> new.recorded_at
       or normalized.confidence <> new.confidence
       or normalized.raw_ref <> new.raw_ref then
        raise exception 'MVP projection does not match normalized record %', new.normalized_record_id
            using errcode = '23514';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.validate_capture_checkpoint_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    identity_payload jsonb;
    content_payload jsonb;
begin
    new.recorded_at_canonical := raw.persisted_canonical_timestamp(new.recorded_at, new.recorded_at_canonical);
    identity_payload := jsonb_build_object('run_id', new.run_id, 'sequence', new.sequence);
    content_payload := identity_payload || jsonb_build_object(
        'phase', new.phase,
        'completed_obligation_ids', to_jsonb(new.completed_obligation_ids),
        'recorded_at', new.recorded_at_canonical
    );
    perform raw.assert_content_address(
        new.checkpoint_id, 'capture-checkpoint', identity_payload, new.content_sha256, content_payload
    );
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.validate_recapture_plan_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    payload jsonb;
    predicate_identity jsonb;
    dimension text;
    bounded boolean := false;
begin
    new.selection_cutoff_canonical := raw.persisted_canonical_timestamp(
        new.selection_cutoff, new.selection_cutoff_canonical
    );
    if jsonb_typeof(new.predicate) <> 'object'
       or (select count(*) from jsonb_object_keys(new.predicate)) <> 12
       or not new.predicate ?& array[
           'predicate_id', 'content_sha256', 'universe_refs', 'subject_ids',
           'source_policy_ids', 'semantic_types', 'partitions', 'terminal_states',
           'freshness_states', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
       ]
       or jsonb_typeof(new.predicate->'predicate_id') <> 'string'
       or new.predicate->>'predicate_id' !~ '^recapture-predicate:[0-9a-f]{64}$'
       or jsonb_typeof(new.predicate->'content_sha256') <> 'string'
       or new.predicate->>'content_sha256' !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(new.predicate->'universe_refs') <> 'array'
       or (
           jsonb_array_length(new.predicate->'universe_refs') > 0
           and not raw.has_canonical_universe_refs(new.predicate->'universe_refs')
       ) then
        raise check_violation using message = 'recapture predicate does not match the typed contract';
    end if;
    foreach dimension in array array[
        'subject_ids', 'source_policy_ids', 'semantic_types', 'partitions', 'terminal_states',
        'freshness_states', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
    ] loop
        if not raw.has_canonical_text_json_array(new.predicate->dimension, true) then
            raise check_violation using message = 'recapture predicate arrays must be canonical';
        end if;
        if dimension = 'terminal_states' and exists (
            select 1 from jsonb_array_elements_text(new.predicate->dimension) as state(value)
             where value not in ('success', 'unchanged', 'unavailable', 'skipped_by_policy', 'failed')
        ) then
            raise check_violation using message = 'recapture terminal state is unknown';
        end if;
        if dimension = 'freshness_states' and exists (
            select 1 from jsonb_array_elements_text(new.predicate->dimension) as state(value)
             where value not in ('fresh', 'stale', 'unknown')
        ) then
            raise check_violation using message = 'recapture freshness state is unknown';
        end if;
        if dimension = any(array[
            'source_policy_ids', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
        ]) and exists (
            select 1
              from jsonb_array_elements_text(new.predicate->dimension) as coordinate(value)
             where lower(value) ~ '(^|[._:/@+\-])(latest|current|default|stable|main|head|tip)($|[._:/@+\-])'
        ) then
            raise check_violation using message = 'recapture predicate version coordinates must not be mutable';
        end if;
        bounded := bounded or jsonb_array_length(new.predicate->dimension) > 0;
    end loop;
    bounded := bounded or jsonb_array_length(new.predicate->'universe_refs') > 0;
    if not bounded then
        raise check_violation using message = 'an unbounded recapture predicate is forbidden';
    end if;
    predicate_identity := new.predicate - 'predicate_id' - 'content_sha256';
    perform raw.assert_content_address(
        new.predicate->>'predicate_id',
        'recapture-predicate',
        jsonb_build_object('kind', 'recapture-predicate', 'identity', predicate_identity),
        new.predicate->>'content_sha256',
        predicate_identity
    );
    if new.predicate_sha256 <> new.predicate->>'content_sha256' then
        raise check_violation using message = 'recapture predicate hash does not match typed content';
    end if;
    if new.planner_version !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
       or lower(new.planner_version) ~
           '(^|[._:/@+\-])(latest|current|default|stable|main|head|tip)($|[._:/@+\-])' then
        raise check_violation using message = 'recapture planner version must not be mutable';
    end if;
    payload := jsonb_build_object(
        'selection_cutoff', new.selection_cutoff_canonical,
        'predicate', new.predicate,
        'selected_obligation_ids', to_jsonb(new.selected_obligation_ids),
        'planner_version', new.planner_version
    );
    perform raw.assert_content_address(
        new.plan_id,
        'capture-list-recapture-plan',
        jsonb_build_object('kind', 'capture-list-recapture-plan', 'identity', payload),
        new.content_sha256,
        payload
    );
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.enforce_capture_checkpoint_progress()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    previous_sequence integer;
    previous_phase text;
    previous_completed text[];
    previous_recorded_at timestamptz;
    previous_phase_rank integer;
    new_phase_rank integer;
begin
    perform pg_advisory_xact_lock(hashtextextended(new.run_id, 0));
    select sequence, phase, completed_obligation_ids, recorded_at
      into previous_sequence, previous_phase, previous_completed, previous_recorded_at
      from raw.capture_checkpoints
     where run_id = new.run_id
     order by sequence desc
     limit 1;
    if previous_sequence is null then
        if new.sequence <> 1 then
            raise exception 'first capture checkpoint sequence must be one';
        end if;
        return new;
    end if;
    if new.sequence <> previous_sequence + 1 then
        raise exception 'capture checkpoint sequences must be contiguous';
    end if;
    previous_phase_rank := array_position(
        array['planned', 'raw_landed', 'normalized', 'manifest_persisted'], previous_phase
    );
    new_phase_rank := array_position(
        array['planned', 'raw_landed', 'normalized', 'manifest_persisted'], new.phase
    );
    if new_phase_rank < previous_phase_rank then
        raise exception 'capture checkpoint phase cannot regress';
    end if;
    if new.recorded_at < previous_recorded_at then
        raise exception 'capture checkpoint time cannot regress';
    end if;
    if not previous_completed <@ new.completed_obligation_ids then
        raise exception 'capture checkpoint obligations cannot regress';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.validate_checkpoint_obligation_refs()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    persisted_count integer;
begin
    if not raw.has_canonical_obligation_ids(new.completed_obligation_ids, true) then
        return new;
    end if;
    select count(*) into persisted_count
      from raw.capture_obligations
     where run_id = new.run_id
       and obligation_id = any(new.completed_obligation_ids);
    if persisted_count <> cardinality(new.completed_obligation_ids) then
        raise exception 'capture checkpoint references an unknown or cross-run obligation';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.validate_recapture_obligation_refs()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    persisted_count integer;
begin
    if not raw.has_canonical_obligation_ids(new.selected_obligation_ids, false) then
        return new;
    end if;
    select count(*) into persisted_count
      from raw.capture_obligations
     where obligation_id = any(new.selected_obligation_ids);
    if persisted_count <> cardinality(new.selected_obligation_ids) then
        raise exception 'recapture plan references an unknown obligation';
    end if;
    return new;
end;
$function$
;

CREATE OR REPLACE FUNCTION raw.has_canonical_obligation_ids(ids text[], allow_empty boolean)
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
declare
    item_index integer;
begin
    if cardinality(ids) = 0 then
        return allow_empty;
    end if;
    for item_index in 1..cardinality(ids) loop
        if ids[item_index] is null or ids[item_index] !~ '^capture-list-obligation:[0-9a-f]{64}$' then
            return false;
        end if;
        if item_index > 1 and ids[item_index - 1] collate "C" >= ids[item_index] collate "C" then
            return false;
        end if;
    end loop;
    return true;
end;
$function$
;


CREATE OR REPLACE FUNCTION raw.has_canonical_text_json_array(values_json jsonb, allow_empty boolean)
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
declare
    item_json jsonb;
    item text;
    previous_item text;
begin
    if jsonb_typeof(values_json) <> 'array' then
        return false;
    end if;
    if jsonb_array_length(values_json) = 0 then
        return allow_empty;
    end if;
    for item_json in select value from jsonb_array_elements(values_json) loop
        if jsonb_typeof(item_json) <> 'string' then
            return false;
        end if;
        item := item_json#>>'{}';
        if item !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
           or (previous_item is not null and previous_item collate "C" >= item collate "C") then
            return false;
        end if;
        previous_item := item;
    end loop;
    return true;
end;
$function$
;

SET search_path = '';

--
-- Name: private_research_objects; Type: TABLE; Schema: app; Owner: -
--

CREATE TABLE app.private_research_objects (
    resource_id text NOT NULL,
    tenant_id text NOT NULL,
    owner_principal_id text NOT NULL,
    resource_type text NOT NULL,
    object_ref text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT private_research_objects_object_ref_check CHECK ((length(object_ref) > 0)),
    CONSTRAINT private_research_objects_resource_id_check CHECK ((length(resource_id) > 0)),
    CONSTRAINT private_research_objects_resource_type_check CHECK ((resource_type = ANY (ARRAY['private_conversation'::text, 'private_document'::text])))
);

ALTER TABLE ONLY app.private_research_objects FORCE ROW LEVEL SECURITY;


--
-- Name: publication_policies; Type: TABLE; Schema: app; Owner: -
--

CREATE TABLE app.publication_policies (
    publication_policy_event_id text NOT NULL,
    publication_policy_id text NOT NULL,
    publication_class_id text NOT NULL,
    permitted boolean NOT NULL,
    successor_policy_id text,
    effective_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT publication_policies_check CHECK ((recorded_at >= effective_at)),
    CONSTRAINT publication_policies_check1 CHECK (((successor_policy_id IS NULL) OR (successor_policy_id <> publication_policy_id))),
    CONSTRAINT publication_policies_publication_class_id_check CHECK ((length(publication_class_id) > 0)),
    CONSTRAINT publication_policies_publication_policy_event_id_check CHECK ((length(publication_policy_event_id) > 0)),
    CONSTRAINT publication_policies_publication_policy_id_check CHECK ((length(publication_policy_id) > 0))
);


--
-- Name: tenant_memberships; Type: TABLE; Schema: app; Owner: -
--

CREATE TABLE app.tenant_memberships (
    membership_event_id text NOT NULL,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    membership_state text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenant_memberships_check CHECK ((recorded_at >= effective_at)),
    CONSTRAINT tenant_memberships_membership_event_id_check CHECK ((length(membership_event_id) > 0)),
    CONSTRAINT tenant_memberships_membership_state_check CHECK ((membership_state = ANY (ARRAY['granted'::text, 'revoked'::text])))
);


--
-- Name: capture_checkpoints; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.capture_checkpoints (
    checkpoint_id text NOT NULL,
    run_id text NOT NULL,
    sequence integer NOT NULL,
    phase text NOT NULL,
    completed_obligation_ids text[] NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    recorded_at_canonical text NOT NULL,
    content_sha256 text NOT NULL,
    CONSTRAINT capture_checkpoints_checkpoint_id_check CHECK ((checkpoint_id ~ '^capture-checkpoint:[0-9a-f]{64}$'::text)),
    CONSTRAINT capture_checkpoints_completed_obligation_ids_check CHECK (raw.has_canonical_obligation_ids(completed_obligation_ids, true)),
    CONSTRAINT capture_checkpoints_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT capture_checkpoints_phase_check CHECK ((phase = ANY (ARRAY['planned'::text, 'raw_landed'::text, 'normalized'::text, 'manifest_persisted'::text]))),
    CONSTRAINT capture_checkpoints_sequence_check CHECK ((sequence > 0))
);


--
-- Name: recapture_plans; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.recapture_plans (
    plan_id text NOT NULL,
    selection_cutoff timestamp with time zone NOT NULL,
    selection_cutoff_canonical text NOT NULL,
    predicate_sha256 text NOT NULL,
    predicate jsonb NOT NULL,
    selected_obligation_ids text[] NOT NULL,
    planner_version text NOT NULL,
    content_sha256 text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recapture_plans_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT recapture_plans_plan_id_check CHECK ((plan_id ~ '^capture-list-recapture-plan:[0-9a-f]{64}$'::text)),
    CONSTRAINT recapture_plans_predicate_check CHECK ((jsonb_typeof(predicate) = 'object'::text)),
    CONSTRAINT recapture_plans_predicate_sha256_check CHECK ((predicate_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT recapture_plans_selected_obligation_ids_check CHECK (raw.has_canonical_obligation_ids(selected_obligation_ids, false))
);


--
-- Name: filing_documents; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.filing_documents (
    normalized_record_id text NOT NULL,
    document_id text NOT NULL,
    issuer_id text NOT NULL,
    accession text NOT NULL,
    form text NOT NULL,
    filing_date date NOT NULL,
    report_period date NOT NULL,
    content_sha256 text NOT NULL,
    content_type text NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT filing_documents_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT filing_documents_content_hash_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT filing_documents_publication_time_check CHECK ((((transaction_time)::date >= filing_date) AND (filing_date >= report_period) AND (recorded_at >= transaction_time))),
    CONSTRAINT filing_documents_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT filing_documents_stable_fields_check CHECK (((document_id <> ''::text) AND (issuer_id <> ''::text) AND (accession <> ''::text) AND (form <> ''::text) AND (content_type <> ''::text))),
    CONSTRAINT filing_documents_valid_time_check CHECK (((NOT isempty(valid_time)) AND (valid_time @> report_period)))
);


--
-- Name: headcount_extraction_invocations; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.headcount_extraction_invocations (
    extraction_invocation_id text NOT NULL,
    content_sha256 text NOT NULL,
    source_document_record_id text NOT NULL,
    document_id text NOT NULL,
    document_sha256 text NOT NULL,
    raw_ref text NOT NULL,
    model_revision_id text NOT NULL,
    model_revision_sha256 text NOT NULL,
    extraction_template_id text NOT NULL,
    extraction_template_sha256 text NOT NULL,
    input_sha256 text NOT NULL,
    response_sha256 text NOT NULL,
    semantic_payload_sha256 text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    invocation jsonb NOT NULL,
    CONSTRAINT headcount_invocation_hashes_check CHECK (((document_sha256 ~ '^[0-9a-f]{64}$'::text) AND (input_sha256 ~ '^[0-9a-f]{64}$'::text) AND (response_sha256 ~ '^[0-9a-f]{64}$'::text) AND (semantic_payload_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT headcount_invocation_id_hash_check CHECK (((extraction_invocation_id = ('extraction-invocation:'::text || content_sha256)) AND (content_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT headcount_invocation_json_check CHECK ((jsonb_typeof(invocation) = 'object'::text)),
    CONSTRAINT headcount_invocation_model_check CHECK (((model_revision_id = ('model-revision:'::text || model_revision_sha256)) AND (model_revision_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT headcount_invocation_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT headcount_invocation_stable_fields_check CHECK ((document_id <> ''::text)),
    CONSTRAINT headcount_invocation_template_check CHECK (((extraction_template_id = ('extraction-template:'::text || extraction_template_sha256)) AND (extraction_template_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT headcount_invocation_time_check CHECK (((completed_at >= started_at) AND (recorded_at >= completed_at)))
);


--
-- Name: headcount_facts; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.headcount_facts (
    normalized_record_id text NOT NULL,
    extraction_invocation_id text NOT NULL,
    issuer_id text NOT NULL,
    availability text NOT NULL,
    value bigint,
    unit text,
    scope text,
    valid_period_end date NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    review_status text NOT NULL,
    unavailable_reason text,
    evidence_spans jsonb NOT NULL,
    payload jsonb NOT NULL,
    record_ref jsonb NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT headcount_facts_availability_check CHECK ((((availability = 'available'::text) AND (value > 0) AND (unit = 'employees'::text) AND (scope = 'total'::text) AND (unavailable_reason IS NULL)) OR ((availability = 'unavailable'::text) AND (value IS NULL) AND (unit IS NULL) AND (scope IS NULL) AND (unavailable_reason IS NOT NULL) AND (unavailable_reason <> ''::text)))),
    CONSTRAINT headcount_facts_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT headcount_facts_json_check CHECK (((jsonb_typeof(evidence_spans) = 'array'::text) AND (jsonb_typeof(payload) = 'object'::text) AND (jsonb_typeof(record_ref) = 'object'::text) AND ((availability = 'unavailable'::text) OR (jsonb_array_length(evidence_spans) > 0)))),
    CONSTRAINT headcount_facts_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT headcount_facts_review_status_check CHECK ((review_status = ANY (ARRAY['reviewed-fixture'::text, 'needs-review'::text, 'rejected'::text]))),
    CONSTRAINT headcount_facts_time_check CHECK ((recorded_at >= transaction_time))
);


--
-- Name: mvp_corporate_actions; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_corporate_actions (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    action_id text NOT NULL,
    action_type text NOT NULL,
    security_id text NOT NULL,
    share_class text NOT NULL,
    source_instrument_ids text[] NOT NULL,
    resulting_instrument_ids text[] NOT NULL,
    source_listing_id text,
    resulting_listing_id text,
    declared_at timestamp with time zone NOT NULL,
    ex_at timestamp with time zone,
    effective_at timestamp with time zone,
    record_at timestamp with time zone,
    pay_at timestamp with time zone,
    split_ratio_after_per_before numeric,
    cash_amount_per_share numeric,
    cash_currency character(3),
    old_symbol text,
    new_symbol text,
    delisting_reason text,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_corporate_actions_check CHECK ((security_id = subject_id)),
    CONSTRAINT mvp_corporate_actions_check1 CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_corporate_actions_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_corporate_actions_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_corporate_actions_subject_kind_check CHECK ((subject_kind = 'security'::text)),
    CONSTRAINT mvp_corporate_actions_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: mvp_financial_facts; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_financial_facts (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    entity_id text NOT NULL,
    metric text NOT NULL,
    value numeric,
    unit text NOT NULL,
    fiscal_period text NOT NULL,
    source_metric text NOT NULL,
    mapping_version text NOT NULL,
    accession text,
    form text,
    is_restatement boolean NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_financial_facts_check CHECK ((entity_id = subject_id)),
    CONSTRAINT mvp_financial_facts_check1 CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_financial_facts_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_financial_facts_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_financial_facts_subject_kind_check CHECK ((subject_kind = 'issuer'::text)),
    CONSTRAINT mvp_financial_facts_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: mvp_issuer_security_links; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_issuer_security_links (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    input_id text NOT NULL,
    issuer_id text NOT NULL,
    security_id text NOT NULL,
    security_kind text NOT NULL,
    share_class text,
    underlying_security_id text,
    underlying_shares_per_security_unit numeric NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_issuer_security_links_check CHECK ((issuer_id = subject_id)),
    CONSTRAINT mvp_issuer_security_links_check1 CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_issuer_security_links_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_issuer_security_links_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_issuer_security_links_subject_kind_check CHECK ((subject_kind = 'issuer'::text)),
    CONSTRAINT mvp_issuer_security_links_underlying_shares_per_security__check CHECK ((underlying_shares_per_security_unit > (0)::numeric)),
    CONSTRAINT mvp_issuer_security_links_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: mvp_market_prices; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_market_prices (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    input_id text NOT NULL,
    issuer_id text NOT NULL,
    security_id text NOT NULL,
    listing_id text NOT NULL,
    share_class text NOT NULL,
    exchange_mic character(4) NOT NULL,
    ticker text NOT NULL,
    calendar_id text NOT NULL,
    calendar_version text NOT NULL,
    trading_date date NOT NULL,
    session_close_at timestamp with time zone NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume bigint NOT NULL,
    currency character(3) NOT NULL,
    price_basis text NOT NULL,
    confidence_policy_id text NOT NULL,
    price_policy_id text NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_market_prices_check CHECK ((listing_id = subject_id)),
    CONSTRAINT mvp_market_prices_check1 CHECK ((valid_time = daterange(trading_date, trading_date, '[]'::text))),
    CONSTRAINT mvp_market_prices_check2 CHECK ((high >= GREATEST(open, low, close))),
    CONSTRAINT mvp_market_prices_check3 CHECK ((low <= LEAST(open, high, close))),
    CONSTRAINT mvp_market_prices_check4 CHECK ((transaction_time >= session_close_at)),
    CONSTRAINT mvp_market_prices_check5 CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_market_prices_close_check CHECK ((close > (0)::numeric)),
    CONSTRAINT mvp_market_prices_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_market_prices_high_check CHECK ((high > (0)::numeric)),
    CONSTRAINT mvp_market_prices_low_check CHECK ((low > (0)::numeric)),
    CONSTRAINT mvp_market_prices_open_check CHECK ((open > (0)::numeric)),
    CONSTRAINT mvp_market_prices_price_basis_check CHECK ((price_basis = 'unadjusted'::text)),
    CONSTRAINT mvp_market_prices_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_market_prices_subject_kind_check CHECK ((subject_kind = 'listing'::text)),
    CONSTRAINT mvp_market_prices_volume_check CHECK ((volume >= 0))
);


--
-- Name: mvp_security_listing_links; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_security_listing_links (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    input_id text NOT NULL,
    security_id text NOT NULL,
    listing_id text NOT NULL,
    exchange_mic character(4) NOT NULL,
    ticker text NOT NULL,
    listing_role text NOT NULL,
    currency character(3) NOT NULL,
    timezone text NOT NULL,
    trading_calendar_id text NOT NULL,
    trading_calendar_version text NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_security_listing_links_check CHECK ((security_id = subject_id)),
    CONSTRAINT mvp_security_listing_links_check1 CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_security_listing_links_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_security_listing_links_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_security_listing_links_subject_kind_check CHECK ((subject_kind = 'security'::text)),
    CONSTRAINT mvp_security_listing_links_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: mvp_universe_memberships; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.mvp_universe_memberships (
    normalized_record_id text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    membership_id text NOT NULL,
    universe_id text NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    raw_ref text NOT NULL,
    CONSTRAINT mvp_universe_memberships_check CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT mvp_universe_memberships_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT mvp_universe_memberships_raw_ref_check CHECK ((raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text)),
    CONSTRAINT mvp_universe_memberships_subject_kind_check CHECK ((subject_kind <> 'universe'::text)),
    CONSTRAINT mvp_universe_memberships_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: normalized_records; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.normalized_records (
    normalized_record_id text NOT NULL,
    content_sha256 text NOT NULL,
    semantic_type_id text NOT NULL,
    semantic_type_version text NOT NULL,
    subject_kind text NOT NULL,
    subject_id text NOT NULL,
    valid_time daterange NOT NULL,
    transaction_time timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    confidence numeric NOT NULL,
    document_id text NOT NULL,
    raw_object_id text NOT NULL,
    raw_object_sha256 text NOT NULL,
    raw_ref text NOT NULL,
    source_registry_entry_id text NOT NULL,
    source_registry_entry_sha256 text NOT NULL,
    mapping_version text NOT NULL,
    mapping_implementation_sha256 text NOT NULL,
    payload_model_key text NOT NULL,
    payload_schema_sha256 text NOT NULL,
    payload_sha256 text NOT NULL,
    payload jsonb NOT NULL,
    record_ref jsonb NOT NULL,
    is_restatement boolean DEFAULT false NOT NULL,
    supersedes_record_id text,
    CONSTRAINT normalized_records_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT normalized_records_hashes_check CHECK (((mapping_implementation_sha256 ~ '^[0-9a-f]{64}$'::text) AND (payload_schema_sha256 ~ '^[0-9a-f]{64}$'::text) AND (payload_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT normalized_records_id_hash_check CHECK (((normalized_record_id = ('normalized-record:'::text || content_sha256)) AND (content_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT normalized_records_payload_shapes_check CHECK (((jsonb_typeof(payload) = 'object'::text) AND (jsonb_typeof(record_ref) = 'object'::text))),
    CONSTRAINT normalized_records_raw_object_check CHECK (((raw_object_sha256 ~ '^[0-9a-f]{64}$'::text) AND (raw_object_id = ('raw-object:'::text || raw_object_sha256)) AND (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'::text))),
    CONSTRAINT normalized_records_restatement_check CHECK (((is_restatement = (supersedes_record_id IS NOT NULL)) AND (supersedes_record_id IS DISTINCT FROM normalized_record_id))),
    CONSTRAINT normalized_records_semantic_type_check CHECK ((semantic_type_id ~ '^semantic\.[a-z0-9]+([._-][a-z0-9]+)*$'::text)),
    CONSTRAINT normalized_records_source_registry_check CHECK (((source_registry_entry_sha256 ~ '^[0-9a-f]{64}$'::text) AND (source_registry_entry_id = ('source-registry-entry:'::text || source_registry_entry_sha256)))),
    CONSTRAINT normalized_records_stable_fields_check CHECK (((semantic_type_version <> ''::text) AND (subject_kind <> ''::text) AND (subject_id <> ''::text) AND (document_id <> ''::text) AND (mapping_version <> ''::text) AND (payload_model_key <> ''::text))),
    CONSTRAINT normalized_records_time_order_check CHECK ((recorded_at >= transaction_time)),
    CONSTRAINT normalized_records_valid_time_check CHECK ((NOT isempty(valid_time)))
);


--
-- Name: private_research_objects private_research_objects_pkey; Type: CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.private_research_objects
    ADD CONSTRAINT private_research_objects_pkey PRIMARY KEY (resource_id);


--
-- Name: publication_policies publication_policies_pkey; Type: CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.publication_policies
    ADD CONSTRAINT publication_policies_pkey PRIMARY KEY (publication_policy_event_id);


--
-- Name: tenant_memberships tenant_memberships_pkey; Type: CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.tenant_memberships
    ADD CONSTRAINT tenant_memberships_pkey PRIMARY KEY (membership_event_id);


--
-- Name: capture_checkpoints capture_checkpoints_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.capture_checkpoints
    ADD CONSTRAINT capture_checkpoints_pkey PRIMARY KEY (checkpoint_id);


--
-- Name: capture_checkpoints capture_checkpoints_run_id_sequence_key; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.capture_checkpoints
    ADD CONSTRAINT capture_checkpoints_run_id_sequence_key UNIQUE (run_id, sequence);


--
-- Name: recapture_plans recapture_plans_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.recapture_plans
    ADD CONSTRAINT recapture_plans_pkey PRIMARY KEY (plan_id);


--
-- Name: filing_documents filing_documents_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.filing_documents
    ADD CONSTRAINT filing_documents_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: headcount_extraction_invocations headcount_extraction_invocations_content_sha256_key; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_extraction_invocations
    ADD CONSTRAINT headcount_extraction_invocations_content_sha256_key UNIQUE (content_sha256);


--
-- Name: headcount_extraction_invocations headcount_extraction_invocations_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_extraction_invocations
    ADD CONSTRAINT headcount_extraction_invocations_pkey PRIMARY KEY (extraction_invocation_id);


--
-- Name: headcount_facts headcount_facts_extraction_invocation_id_key; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_facts
    ADD CONSTRAINT headcount_facts_extraction_invocation_id_key UNIQUE (extraction_invocation_id);


--
-- Name: headcount_facts headcount_facts_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_facts
    ADD CONSTRAINT headcount_facts_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_corporate_actions mvp_corporate_actions_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_corporate_actions
    ADD CONSTRAINT mvp_corporate_actions_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_financial_facts mvp_financial_facts_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_financial_facts
    ADD CONSTRAINT mvp_financial_facts_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_issuer_security_links mvp_issuer_security_links_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_issuer_security_links
    ADD CONSTRAINT mvp_issuer_security_links_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_market_prices mvp_market_prices_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_market_prices
    ADD CONSTRAINT mvp_market_prices_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_security_listing_links mvp_security_listing_links_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_security_listing_links
    ADD CONSTRAINT mvp_security_listing_links_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: mvp_universe_memberships mvp_universe_memberships_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_universe_memberships
    ADD CONSTRAINT mvp_universe_memberships_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: normalized_records normalized_records_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.normalized_records
    ADD CONSTRAINT normalized_records_pkey PRIMARY KEY (normalized_record_id);


--
-- Name: idx_private_research_objects_owner; Type: INDEX; Schema: app; Owner: -
--

CREATE INDEX idx_private_research_objects_owner ON app.private_research_objects USING btree (tenant_id, owner_principal_id, resource_id);


--
-- Name: idx_filing_documents_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_filing_documents_asof ON staging.filing_documents USING btree (issuer_id, report_period, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_headcount_facts_pit; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_headcount_facts_pit ON staging.headcount_facts USING btree (issuer_id, valid_period_end, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_headcount_invocation_document; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_headcount_invocation_document ON staging.headcount_extraction_invocations USING btree (source_document_record_id, started_at, extraction_invocation_id);


--
-- Name: idx_mvp_corporate_actions_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_corporate_actions_asof ON staging.mvp_corporate_actions USING btree (security_id, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_mvp_financial_facts_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_financial_facts_asof ON staging.mvp_financial_facts USING btree (entity_id, metric, fiscal_period, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_mvp_issuer_security_links_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_issuer_security_links_asof ON staging.mvp_issuer_security_links USING btree (issuer_id, security_id, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_mvp_market_prices_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_market_prices_asof ON staging.mvp_market_prices USING btree (listing_id, trading_date, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_mvp_security_listing_links_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_security_listing_links_asof ON staging.mvp_security_listing_links USING btree (security_id, listing_id, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_mvp_universe_memberships_asof; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_mvp_universe_memberships_asof ON staging.mvp_universe_memberships USING btree (universe_id, subject_kind, subject_id, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_normalized_records_registry_snapshot; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_normalized_records_registry_snapshot ON staging.normalized_records USING btree (source_registry_entry_id, semantic_type_id, semantic_type_version, subject_kind, subject_id, transaction_time DESC);


--
-- Name: idx_normalized_records_snapshot; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_normalized_records_snapshot ON staging.normalized_records USING btree (semantic_type_id, semantic_type_version, subject_kind, subject_id, transaction_time DESC, recorded_at DESC);


--
-- Name: idx_normalized_records_valid_time; Type: INDEX; Schema: staging; Owner: -
--

CREATE INDEX idx_normalized_records_valid_time ON staging.normalized_records USING gist (valid_time);


--
-- Name: uq_normalized_records_content; Type: INDEX; Schema: staging; Owner: -
--

CREATE UNIQUE INDEX uq_normalized_records_content ON staging.normalized_records USING btree (content_sha256);


--
-- Name: uq_normalized_records_single_successor; Type: INDEX; Schema: staging; Owner: -
--

CREATE UNIQUE INDEX uq_normalized_records_single_successor ON staging.normalized_records USING btree (supersedes_record_id) WHERE (supersedes_record_id IS NOT NULL);


--
-- Name: private_research_objects trg_private_research_objects_append_only; Type: TRIGGER; Schema: app; Owner: -
--

CREATE TRIGGER trg_private_research_objects_append_only BEFORE DELETE OR UPDATE ON app.private_research_objects FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();


--
-- Name: publication_policies trg_publication_policies_append_only; Type: TRIGGER; Schema: app; Owner: -
--

CREATE TRIGGER trg_publication_policies_append_only BEFORE DELETE OR UPDATE ON app.publication_policies FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();


--
-- Name: tenant_memberships trg_tenant_memberships_append_only; Type: TRIGGER; Schema: app; Owner: -
--

CREATE TRIGGER trg_tenant_memberships_append_only BEFORE DELETE OR UPDATE ON app.tenant_memberships FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();


--
-- Name: capture_checkpoints enforce_checkpoint_progress; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER enforce_checkpoint_progress BEFORE INSERT ON raw.capture_checkpoints FOR EACH ROW EXECUTE FUNCTION raw.enforce_capture_checkpoint_progress();


--
-- Name: capture_checkpoints reject_mutation; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_checkpoints FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation();


--
-- Name: recapture_plans reject_mutation; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.recapture_plans FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation();


--
-- Name: capture_checkpoints validate_checkpoint_obligation_refs; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER validate_checkpoint_obligation_refs BEFORE INSERT ON raw.capture_checkpoints FOR EACH ROW EXECUTE FUNCTION raw.validate_checkpoint_obligation_refs();


--
-- Name: recapture_plans validate_recapture_obligation_refs; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER validate_recapture_obligation_refs BEFORE INSERT ON raw.recapture_plans FOR EACH ROW EXECUTE FUNCTION raw.validate_recapture_obligation_refs();


--
-- Name: capture_checkpoints zz_validate_checkpoint_address; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER zz_validate_checkpoint_address BEFORE INSERT ON raw.capture_checkpoints FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_checkpoint_address();


--
-- Name: recapture_plans zz_validate_recapture_plan_address; Type: TRIGGER; Schema: raw; Owner: -
--

CREATE TRIGGER zz_validate_recapture_plan_address BEFORE INSERT ON raw.recapture_plans FOR EACH ROW EXECUTE FUNCTION raw.validate_recapture_plan_address();


--
-- Name: filing_documents trg_filing_documents_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_filing_documents_append_only BEFORE DELETE OR UPDATE ON staging.filing_documents FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: filing_documents trg_filing_documents_validate_projection; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_filing_documents_validate_projection BEFORE INSERT ON staging.filing_documents FOR EACH ROW EXECUTE FUNCTION staging.validate_filing_document_projection();


--
-- Name: headcount_facts trg_headcount_facts_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_headcount_facts_append_only BEFORE DELETE OR UPDATE ON staging.headcount_facts FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: headcount_facts trg_headcount_facts_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_headcount_facts_validate BEFORE INSERT ON staging.headcount_facts FOR EACH ROW EXECUTE FUNCTION staging.validate_headcount_projection();


--
-- Name: headcount_extraction_invocations trg_headcount_invocations_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_headcount_invocations_append_only BEFORE DELETE OR UPDATE ON staging.headcount_extraction_invocations FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: headcount_extraction_invocations trg_headcount_invocations_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_headcount_invocations_validate BEFORE INSERT ON staging.headcount_extraction_invocations FOR EACH ROW EXECUTE FUNCTION staging.validate_headcount_invocation();


--
-- Name: mvp_corporate_actions trg_mvp_corporate_actions_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_corporate_actions_append_only BEFORE DELETE OR UPDATE ON staging.mvp_corporate_actions FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_corporate_actions trg_mvp_corporate_actions_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_corporate_actions_validate BEFORE INSERT ON staging.mvp_corporate_actions FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.corporate-action');


--
-- Name: mvp_financial_facts trg_mvp_financial_facts_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_financial_facts_append_only BEFORE DELETE OR UPDATE ON staging.mvp_financial_facts FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_financial_facts trg_mvp_financial_facts_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_financial_facts_validate BEFORE INSERT ON staging.mvp_financial_facts FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.financial-fact');


--
-- Name: mvp_issuer_security_links trg_mvp_issuer_security_links_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_issuer_security_links_append_only BEFORE DELETE OR UPDATE ON staging.mvp_issuer_security_links FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_issuer_security_links trg_mvp_issuer_security_links_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_issuer_security_links_validate BEFORE INSERT ON staging.mvp_issuer_security_links FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.issuer-security-link');


--
-- Name: mvp_market_prices trg_mvp_market_prices_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_market_prices_append_only BEFORE DELETE OR UPDATE ON staging.mvp_market_prices FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_market_prices trg_mvp_market_prices_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_market_prices_validate BEFORE INSERT ON staging.mvp_market_prices FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.market-price');


--
-- Name: mvp_security_listing_links trg_mvp_security_listing_links_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_security_listing_links_append_only BEFORE DELETE OR UPDATE ON staging.mvp_security_listing_links FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_security_listing_links trg_mvp_security_listing_links_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_security_listing_links_validate BEFORE INSERT ON staging.mvp_security_listing_links FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.security-listing-link');


--
-- Name: mvp_universe_memberships trg_mvp_universe_memberships_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_universe_memberships_append_only BEFORE DELETE OR UPDATE ON staging.mvp_universe_memberships FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: mvp_universe_memberships trg_mvp_universe_memberships_validate; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_mvp_universe_memberships_validate BEFORE INSERT ON staging.mvp_universe_memberships FOR EACH ROW EXECUTE FUNCTION staging.validate_mvp_projection('semantic.universe-membership');


--
-- Name: normalized_records trg_normalized_records_append_only; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_normalized_records_append_only BEFORE DELETE OR UPDATE ON staging.normalized_records FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation();


--
-- Name: normalized_records trg_normalized_records_validate_raw_lineage; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_normalized_records_validate_raw_lineage BEFORE INSERT ON staging.normalized_records FOR EACH ROW EXECUTE FUNCTION staging.validate_normalized_raw_lineage();


--
-- Name: normalized_records trg_normalized_records_validate_restatement; Type: TRIGGER; Schema: staging; Owner: -
--

CREATE TRIGGER trg_normalized_records_validate_restatement BEFORE INSERT ON staging.normalized_records FOR EACH ROW EXECUTE FUNCTION staging.validate_normalized_restatement();


--
-- Name: private_research_objects private_research_objects_owner_principal_id_fkey; Type: FK CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.private_research_objects
    ADD CONSTRAINT private_research_objects_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id);


--
-- Name: private_research_objects private_research_objects_tenant_id_fkey; Type: FK CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.private_research_objects
    ADD CONSTRAINT private_research_objects_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id);


--
-- Name: tenant_memberships tenant_memberships_principal_id_fkey; Type: FK CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.tenant_memberships
    ADD CONSTRAINT tenant_memberships_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES app.principals(principal_id);


--
-- Name: tenant_memberships tenant_memberships_tenant_id_fkey; Type: FK CONSTRAINT; Schema: app; Owner: -
--

ALTER TABLE ONLY app.tenant_memberships
    ADD CONSTRAINT tenant_memberships_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id);


--
-- Name: capture_checkpoints capture_checkpoints_run_id_fkey; Type: FK CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.capture_checkpoints
    ADD CONSTRAINT capture_checkpoints_run_id_fkey FOREIGN KEY (run_id) REFERENCES raw.capture_runs(run_id);


--
-- Name: filing_documents filing_documents_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.filing_documents
    ADD CONSTRAINT filing_documents_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: headcount_extraction_invocations headcount_extraction_invocations_source_document_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_extraction_invocations
    ADD CONSTRAINT headcount_extraction_invocations_source_document_record_id_fkey FOREIGN KEY (source_document_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: headcount_facts headcount_facts_extraction_invocation_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_facts
    ADD CONSTRAINT headcount_facts_extraction_invocation_id_fkey FOREIGN KEY (extraction_invocation_id) REFERENCES staging.headcount_extraction_invocations(extraction_invocation_id);


--
-- Name: headcount_facts headcount_facts_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.headcount_facts
    ADD CONSTRAINT headcount_facts_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_corporate_actions mvp_corporate_actions_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_corporate_actions
    ADD CONSTRAINT mvp_corporate_actions_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_financial_facts mvp_financial_facts_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_financial_facts
    ADD CONSTRAINT mvp_financial_facts_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_issuer_security_links mvp_issuer_security_links_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_issuer_security_links
    ADD CONSTRAINT mvp_issuer_security_links_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_market_prices mvp_market_prices_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_market_prices
    ADD CONSTRAINT mvp_market_prices_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_security_listing_links mvp_security_listing_links_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_security_listing_links
    ADD CONSTRAINT mvp_security_listing_links_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: mvp_universe_memberships mvp_universe_memberships_normalized_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.mvp_universe_memberships
    ADD CONSTRAINT mvp_universe_memberships_normalized_record_id_fkey FOREIGN KEY (normalized_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: normalized_records normalized_records_supersedes_record_id_fkey; Type: FK CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.normalized_records
    ADD CONSTRAINT normalized_records_supersedes_record_id_fkey FOREIGN KEY (supersedes_record_id) REFERENCES staging.normalized_records(normalized_record_id);


--
-- Name: private_research_objects; Type: ROW SECURITY; Schema: app; Owner: -
--

ALTER TABLE app.private_research_objects ENABLE ROW LEVEL SECURITY;

--
-- Name: private_research_objects private_research_owner_isolation; Type: POLICY; Schema: app; Owner: -
--

CREATE POLICY private_research_owner_isolation ON app.private_research_objects FOR SELECT USING (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));


--
-- PostgreSQL database dump complete
--


