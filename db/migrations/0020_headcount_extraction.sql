-- H0 E0: append-only extraction invocation and typed headcount projection.

create table if not exists staging.headcount_extraction_invocations (
    extraction_invocation_id   text primary key,
    content_sha256             text not null unique,
    source_document_record_id  text not null
        references staging.normalized_records(normalized_record_id),
    document_id                text not null,
    document_sha256            text not null,
    raw_ref                    text not null,
    model_revision_id          text not null,
    model_revision_sha256      text not null,
    extraction_template_id     text not null,
    extraction_template_sha256 text not null,
    input_sha256               text not null,
    response_sha256            text not null,
    semantic_payload_sha256    text not null,
    started_at                 timestamptz not null,
    completed_at               timestamptz not null,
    recorded_at                timestamptz not null,
    invocation                 jsonb not null,
    constraint headcount_invocation_id_hash_check check (
        extraction_invocation_id = 'extraction-invocation:' || content_sha256
        and content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint headcount_invocation_model_check check (
        model_revision_id = 'model-revision:' || model_revision_sha256
        and model_revision_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint headcount_invocation_template_check check (
        extraction_template_id = 'extraction-template:' || extraction_template_sha256
        and extraction_template_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint headcount_invocation_hashes_check check (
        document_sha256 ~ '^[0-9a-f]{64}$'
        and input_sha256 ~ '^[0-9a-f]{64}$'
        and response_sha256 ~ '^[0-9a-f]{64}$'
        and semantic_payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint headcount_invocation_stable_fields_check check (document_id <> ''),
    constraint headcount_invocation_raw_ref_check check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    constraint headcount_invocation_time_check check (
        completed_at >= started_at
        and recorded_at >= completed_at
    ),
    constraint headcount_invocation_json_check check (jsonb_typeof(invocation) = 'object')
);

create index if not exists idx_headcount_invocation_document
    on staging.headcount_extraction_invocations (
        source_document_record_id,
        started_at,
        extraction_invocation_id
    );

create table if not exists staging.headcount_facts (
    normalized_record_id      text primary key
        references staging.normalized_records(normalized_record_id),
    extraction_invocation_id text not null unique
        references staging.headcount_extraction_invocations(extraction_invocation_id),
    issuer_id                text not null,
    availability             text not null,
    value                    bigint,
    unit                     text,
    scope                    text,
    valid_period_end         date not null,
    transaction_time         timestamptz not null,
    recorded_at              timestamptz not null,
    confidence               numeric not null,
    review_status            text not null,
    unavailable_reason       text,
    evidence_spans           jsonb not null,
    payload                  jsonb not null,
    record_ref               jsonb not null,
    raw_ref                  text not null,
    constraint headcount_facts_availability_check check (
        (
            availability = 'available'
            and value > 0
            and unit = 'employees'
            and scope = 'total'
            and unavailable_reason is null
        )
        or (
            availability = 'unavailable'
            and value is null
            and unit is null
            and scope is null
            and unavailable_reason is not null
            and unavailable_reason <> ''
        )
    ),
    constraint headcount_facts_confidence_check check (confidence >= 0 and confidence <= 1),
    constraint headcount_facts_review_status_check check (
        review_status in ('reviewed-fixture', 'needs-review', 'rejected')
    ),
    constraint headcount_facts_time_check check (recorded_at >= transaction_time),
    constraint headcount_facts_raw_ref_check check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    constraint headcount_facts_json_check check (
        jsonb_typeof(evidence_spans) = 'array'
        and jsonb_typeof(payload) = 'object'
        and jsonb_typeof(record_ref) = 'object'
        and (
            availability = 'unavailable'
            or jsonb_array_length(evidence_spans) > 0
        )
    )
);

create index if not exists idx_headcount_facts_pit
    on staging.headcount_facts (
        issuer_id,
        valid_period_end,
        transaction_time desc,
        recorded_at desc
    );

create or replace function staging.validate_headcount_invocation()
returns trigger language plpgsql as $$
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
$$;

create or replace function staging.validate_headcount_projection()
returns trigger language plpgsql as $$
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
$$;

drop trigger if exists trg_headcount_invocations_validate
    on staging.headcount_extraction_invocations;
create trigger trg_headcount_invocations_validate
before insert on staging.headcount_extraction_invocations
for each row execute function staging.validate_headcount_invocation();

drop trigger if exists trg_headcount_facts_validate
    on staging.headcount_facts;
create trigger trg_headcount_facts_validate
before insert on staging.headcount_facts
for each row execute function staging.validate_headcount_projection();

drop trigger if exists trg_headcount_invocations_append_only
    on staging.headcount_extraction_invocations;
create trigger trg_headcount_invocations_append_only
before update or delete on staging.headcount_extraction_invocations
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_headcount_facts_append_only
    on staging.headcount_facts;
create trigger trg_headcount_facts_append_only
before update or delete on staging.headcount_facts
for each row execute function staging.reject_point_in_time_mutation();
