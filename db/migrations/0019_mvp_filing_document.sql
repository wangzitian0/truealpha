-- D1 E0: durable normalized-record spine and filing-document projection.
--
-- `staging.normalized_records` preserves the complete #58 record envelope while
-- typed projection tables provide queryable domain columns. Both layers are
-- append-only. Cross-table triggers prevent a typed row from weakening the PIT,
-- confidence, or raw-lineage evidence held by its normalized envelope.

create table if not exists staging.normalized_records (
    normalized_record_id          text primary key,
    content_sha256                text not null,
    semantic_type_id              text not null,
    semantic_type_version         text not null,
    subject_kind                  text not null,
    subject_id                    text not null,
    valid_time                    daterange not null,
    transaction_time              timestamptz not null,
    recorded_at                   timestamptz not null,
    confidence                    numeric not null,
    document_id                   text not null,
    raw_object_id                 text not null,
    raw_object_sha256             text not null,
    raw_ref                       text not null,
    source_registry_entry_id      text not null,
    source_registry_entry_sha256  text not null,
    mapping_version               text not null,
    mapping_implementation_sha256 text not null,
    payload_model_key             text not null,
    payload_schema_sha256         text not null,
    payload_sha256                text not null,
    payload                       jsonb not null,
    record_ref                    jsonb not null,
    is_restatement                boolean not null default false,
    supersedes_record_id          text references staging.normalized_records(normalized_record_id),
    constraint normalized_records_id_hash_check check (
        normalized_record_id = 'normalized-record:' || content_sha256
        and content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint normalized_records_semantic_type_check check (
        semantic_type_id ~ '^semantic\.[a-z0-9]+([._-][a-z0-9]+)*$'
    ),
    constraint normalized_records_stable_fields_check check (
        semantic_type_version <> ''
        and subject_kind <> ''
        and subject_id <> ''
        and document_id <> ''
        and mapping_version <> ''
        and payload_model_key <> ''
    ),
    constraint normalized_records_valid_time_check check (not isempty(valid_time)),
    constraint normalized_records_time_order_check check (recorded_at >= transaction_time),
    constraint normalized_records_confidence_check check (confidence >= 0 and confidence <= 1),
    constraint normalized_records_raw_object_check check (
        raw_object_sha256 ~ '^[0-9a-f]{64}$'
        and raw_object_id = 'raw-object:' || raw_object_sha256
        and raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'
    ),
    constraint normalized_records_source_registry_check check (
        source_registry_entry_sha256 ~ '^[0-9a-f]{64}$'
        and source_registry_entry_id = 'source-registry-entry:' || source_registry_entry_sha256
    ),
    constraint normalized_records_hashes_check check (
        mapping_implementation_sha256 ~ '^[0-9a-f]{64}$'
        and payload_schema_sha256 ~ '^[0-9a-f]{64}$'
        and payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint normalized_records_payload_shapes_check check (
        jsonb_typeof(payload) = 'object'
        and jsonb_typeof(record_ref) = 'object'
    ),
    constraint normalized_records_restatement_check check (
        is_restatement = (supersedes_record_id is not null)
        and supersedes_record_id is distinct from normalized_record_id
    )
);

create unique index if not exists uq_normalized_records_content
    on staging.normalized_records (content_sha256);

create unique index if not exists uq_normalized_records_single_successor
    on staging.normalized_records (supersedes_record_id)
    where supersedes_record_id is not null;

create index if not exists idx_normalized_records_snapshot
    on staging.normalized_records (
        semantic_type_id,
        semantic_type_version,
        subject_kind,
        subject_id,
        transaction_time desc,
        recorded_at desc
    );

create index if not exists idx_normalized_records_valid_time
    on staging.normalized_records using gist (valid_time);

create index if not exists idx_normalized_records_registry_snapshot
    on staging.normalized_records (
        source_registry_entry_id,
        semantic_type_id,
        semantic_type_version,
        subject_kind,
        subject_id,
        transaction_time desc
    );

create table if not exists staging.filing_documents (
    normalized_record_id text primary key
        references staging.normalized_records(normalized_record_id),
    document_id          text not null,
    issuer_id            text not null,
    accession            text not null,
    form                 text not null,
    filing_date          date not null,
    report_period        date not null,
    content_sha256       text not null,
    content_type         text not null,
    valid_time           daterange not null,
    transaction_time     timestamptz not null,
    recorded_at          timestamptz not null,
    confidence           numeric not null,
    raw_ref              text not null,
    constraint filing_documents_stable_fields_check check (
        document_id <> ''
        and issuer_id <> ''
        and accession <> ''
        and form <> ''
        and content_type <> ''
    ),
    constraint filing_documents_content_hash_check check (content_sha256 ~ '^[0-9a-f]{64}$'),
    constraint filing_documents_valid_time_check check (
        not isempty(valid_time)
        and valid_time @> report_period
    ),
    constraint filing_documents_publication_time_check check (
        transaction_time::date >= filing_date
        and filing_date >= report_period
        and recorded_at >= transaction_time
    ),
    constraint filing_documents_confidence_check check (confidence >= 0 and confidence <= 1),
    constraint filing_documents_raw_ref_check check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$')
);

-- A reviewed normalizer revision may append a new normalized identity over the
-- same immutable raw fetch. The normalized-record primary key, not the source
-- coordinate, is therefore the projection identity.
alter table staging.filing_documents
    drop constraint if exists filing_documents_vintage_unique;

create index if not exists idx_filing_documents_asof
    on staging.filing_documents (issuer_id, report_period, transaction_time desc, recorded_at desc);

create or replace function staging.validate_normalized_raw_lineage()
returns trigger language plpgsql as $$
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
$$;

create or replace function staging.validate_filing_document_projection()
returns trigger language plpgsql as $$
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
$$;

create or replace function staging.validate_normalized_restatement()
returns trigger language plpgsql as $$
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
$$;

drop trigger if exists trg_normalized_records_validate_raw_lineage
    on staging.normalized_records;
create trigger trg_normalized_records_validate_raw_lineage
before insert on staging.normalized_records
for each row execute function staging.validate_normalized_raw_lineage();

drop trigger if exists trg_normalized_records_validate_restatement
    on staging.normalized_records;
create trigger trg_normalized_records_validate_restatement
before insert on staging.normalized_records
for each row execute function staging.validate_normalized_restatement();

drop trigger if exists trg_filing_documents_validate_projection
    on staging.filing_documents;
create trigger trg_filing_documents_validate_projection
before insert on staging.filing_documents
for each row execute function staging.validate_filing_document_projection();

drop trigger if exists trg_normalized_records_append_only
    on staging.normalized_records;
create trigger trg_normalized_records_append_only
before update or delete on staging.normalized_records
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_filing_documents_append_only
    on staging.filing_documents;
create trigger trg_filing_documents_append_only
before update or delete on staging.filing_documents
for each row execute function staging.reject_point_in_time_mutation();
