begin;

do $$
declare
    default_expression text;
    required_column_count integer;
    required_trigger_count integer;
begin
    if to_regclass('raw.fetches') is null then
        raise exception 'raw.fetches is missing';
    end if;
    if to_regclass('staging.kg_edges') is null then
        raise exception 'Postgres graph store is missing';
    end if;
    if to_regclass('staging.kg_identifiers') is null then
        raise exception 'Postgres graph identifier index is missing';
    end if;
    if to_regclass('staging.contract_objects') is null then
        raise exception 'immutable Gate 0 contract repository is missing';
    end if;
    if to_regclass('staging.normalized_records') is null then
        raise exception 'append-only normalized record repository is missing';
    end if;
    if to_regclass('staging.filing_documents') is null then
        raise exception 'filing document projection is missing';
    end if;

    select count(*) into required_column_count
    from information_schema.columns
    where table_schema = 'staging'
      and (
        (
            table_name = 'normalized_records'
            and column_name in ('valid_time', 'transaction_time', 'recorded_at', 'confidence', 'raw_ref')
            and is_nullable = 'NO'
            and column_default is null
        )
        or (
            table_name = 'filing_documents'
            and column_name in ('valid_time', 'transaction_time', 'recorded_at', 'confidence', 'raw_ref')
            and is_nullable = 'NO'
            and column_default is null
        )
      );
    if required_column_count <> 10 then
        raise exception 'normalized filing PIT, confidence, or raw-lineage columns are not mandatory and explicit';
    end if;

    select count(*) into required_trigger_count
    from pg_trigger
    where not tgisinternal
      and tgname in (
        'trg_normalized_records_validate_raw_lineage',
        'trg_normalized_records_validate_restatement',
        'trg_normalized_records_append_only',
        'trg_filing_documents_validate_projection',
        'trg_filing_documents_append_only'
      )
      and tgrelid in (
        'staging.normalized_records'::regclass,
        'staging.filing_documents'::regclass
      );
    if required_trigger_count <> 5 then
        raise exception 'normalized filing validation or append-only triggers are incomplete';
    end if;
    if to_regclass('staging.uq_normalized_records_single_successor') is null then
        raise exception 'normalized restatement lineage can branch';
    end if;
    if exists (
        select 1
        from pg_constraint
        where conrelid = 'staging.filing_documents'::regclass
          and conname = 'filing_documents_vintage_unique'
    ) then
        raise exception 'filing projection blocks replay under a new normalized identity';
    end if;
    if to_regclass('staging.idx_normalized_records_registry_snapshot') is null then
        raise exception 'registry-bound normalized PIT lookup is not indexed';
    end if;

    select column_default into default_expression
    from information_schema.columns
    where table_schema = 'staging'
      and table_name = 'financial_facts'
      and column_name = 'transaction_time';
    if default_expression is not null then
        raise exception 'financial_facts.transaction_time must be explicit';
    end if;
end;
$$;

insert into raw.fetches (
    source,
    source_record_id,
    payload_sha256,
    object_uri,
    content_type,
    byte_length,
    fetched_at
) values (
    'sec',
    'contract-test',
    repeat('a', 64),
    's3://truealpha-raw/raw/sec/aa/test',
    'application/json',
    2,
    now()
);

do $$
declare
    mutation_rejected boolean := false;
begin
    begin
        update raw.fetches set byte_length = 3 where source_record_id = 'contract-test';
    exception when raise_exception then
        mutation_rejected := true;
    end;
    if not mutation_rejected then
        raise exception 'raw.fetches accepted an update';
    end if;
end;
$$;

insert into staging.normalized_records (
    normalized_record_id,
    content_sha256,
    semantic_type_id,
    semantic_type_version,
    subject_kind,
    subject_id,
    valid_time,
    transaction_time,
    recorded_at,
    confidence,
    document_id,
    raw_object_id,
    raw_object_sha256,
    raw_ref,
    source_registry_entry_id,
    source_registry_entry_sha256,
    mapping_version,
    mapping_implementation_sha256,
    payload_model_key,
    payload_schema_sha256,
    payload_sha256,
    payload,
    record_ref
) select
    'normalized-record:' || repeat('b', 64),
    repeat('b', 64),
    'semantic.filing-document',
    '1.0.0',
    'issuer',
    'issuer.contract-test',
    daterange('2020-01-01', '2021-01-01', '[)'),
    timestamptz '2021-03-01 12:00:00+00',
    greatest(recorded_at, timestamptz '2021-03-01 12:00:00+00'),
    1.0,
    'document:contract-test',
    'raw-object:' || repeat('a', 64),
    repeat('a', 64),
    'raw.fetches:' || id,
    'source-registry-entry:' || repeat('c', 64),
    repeat('c', 64),
    'normalizer.contract-test:1.0.0',
    repeat('d', 64),
    'truealpha:FilingDocument',
    repeat('e', 64),
    repeat('f', 64),
    '{}'::jsonb,
    '{}'::jsonb
from raw.fetches
where source_record_id = 'contract-test';

insert into staging.filing_documents (
    normalized_record_id,
    document_id,
    issuer_id,
    accession,
    form,
    filing_date,
    report_period,
    content_sha256,
    content_type,
    valid_time,
    transaction_time,
    recorded_at,
    confidence,
    raw_ref
)
select
    normalized_record_id,
    document_id,
    subject_id,
    '0000000000-21-000001',
    '10-K',
    date '2021-03-01',
    date '2020-12-31',
    raw_object_sha256,
    'text/html',
    valid_time,
    transaction_time,
    recorded_at,
    confidence,
    raw_ref
from staging.normalized_records
where normalized_record_id = 'normalized-record:' || repeat('b', 64);

do $$
declare
    bad_confidence_rejected boolean := false;
    bad_lineage_rejected boolean := false;
    bad_projection_rejected boolean := false;
    normalized_update_rejected boolean := false;
    normalized_delete_rejected boolean := false;
    filing_update_rejected boolean := false;
    filing_delete_rejected boolean := false;
begin
    begin
        insert into staging.normalized_records (
            normalized_record_id, content_sha256, semantic_type_id, semantic_type_version,
            subject_kind, subject_id, valid_time, transaction_time, recorded_at,
            confidence, document_id, raw_object_id, raw_object_sha256, raw_ref,
            source_registry_entry_id, source_registry_entry_sha256, mapping_version,
            mapping_implementation_sha256, payload_model_key, payload_schema_sha256,
            payload_sha256, payload, record_ref
        ) values (
            'normalized-record:' || repeat('1', 64), repeat('1', 64),
            'semantic.filing-document', '1.0.0', 'issuer', 'issuer.invalid',
            daterange('2020-01-01', '2021-01-01', '[)'), now(), now(), 2,
            'document:invalid', 'raw-object:' || repeat('a', 64), repeat('a', 64),
            (select 'raw.fetches:' || id from raw.fetches where source_record_id = 'contract-test'),
            'source-registry-entry:' || repeat('c', 64), repeat('c', 64), 'invalid:1.0.0',
            repeat('d', 64), 'truealpha:FilingDocument', repeat('e', 64), repeat('f', 64),
            '{}'::jsonb, '{}'::jsonb
        );
    exception when check_violation then
        bad_confidence_rejected := true;
    end;

    begin
        insert into staging.normalized_records (
            normalized_record_id, content_sha256, semantic_type_id, semantic_type_version,
            subject_kind, subject_id, valid_time, transaction_time, recorded_at,
            confidence, document_id, raw_object_id, raw_object_sha256, raw_ref,
            source_registry_entry_id, source_registry_entry_sha256, mapping_version,
            mapping_implementation_sha256, payload_model_key, payload_schema_sha256,
            payload_sha256, payload, record_ref
        ) values (
            'normalized-record:' || repeat('2', 64), repeat('2', 64),
            'semantic.filing-document', '1.0.0', 'issuer', 'issuer.invalid',
            daterange('2020-01-01', '2021-01-01', '[)'), now(), now(), 1,
            'document:invalid', 'raw-object:' || repeat('9', 64), repeat('9', 64),
            (select 'raw.fetches:' || id from raw.fetches where source_record_id = 'contract-test'),
            'source-registry-entry:' || repeat('c', 64), repeat('c', 64), 'invalid:1.0.0',
            repeat('d', 64), 'truealpha:FilingDocument', repeat('e', 64), repeat('f', 64),
            '{}'::jsonb, '{}'::jsonb
        );
    exception when check_violation then
        bad_lineage_rejected := true;
    end;

    begin
        insert into staging.filing_documents (
            normalized_record_id, document_id, issuer_id, accession, form,
            filing_date, report_period, content_sha256, content_type, valid_time,
            transaction_time, recorded_at, confidence, raw_ref
        )
        select normalized_record_id, document_id, 'issuer.wrong', '0000000000-21-000001',
               '10-K', date '2021-03-01', date '2020-12-31', raw_object_sha256,
               'text/html', valid_time, transaction_time, recorded_at, confidence, raw_ref
        from staging.normalized_records
        where normalized_record_id = 'normalized-record:' || repeat('b', 64);
    exception when check_violation then
        bad_projection_rejected := true;
    end;

    begin
        update staging.normalized_records set confidence = 0.9;
    exception when raise_exception then
        normalized_update_rejected := true;
    end;
    begin
        delete from staging.normalized_records;
    exception when raise_exception then
        normalized_delete_rejected := true;
    end;
    begin
        update staging.filing_documents set confidence = 0.9;
    exception when raise_exception then
        filing_update_rejected := true;
    end;
    begin
        delete from staging.filing_documents;
    exception when raise_exception then
        filing_delete_rejected := true;
    end;

    if not bad_confidence_rejected or not bad_lineage_rejected or not bad_projection_rejected then
        raise exception 'normalized filing constraints accepted invalid evidence';
    end if;
    if not normalized_update_rejected or not normalized_delete_rejected
       or not filing_update_rejected or not filing_delete_rejected then
        raise exception 'normalized filing tables are not append-only';
    end if;
end;
$$;

insert into staging.contract_objects (
    contract_id,
    contract_kind,
    content_sha256,
    payload
) values
    ('registry-snapshot:' || repeat('b', 64), 'registry_snapshot', repeat('b', 64), '{}'::jsonb),
    (
        'research-catalog:' || repeat('c', 64),
        'research_catalog_manifest',
        repeat('c', 64),
        '{}'::jsonb
    ),
    ('snapshot:' || repeat('d', 64), 'snapshot_manifest', repeat('d', 64), '{}'::jsonb),
    ('release-manifest:' || repeat('e', 64), 'release_manifest', repeat('e', 64), '{}'::jsonb),
    ('capture-scope:' || repeat('f', 64), 'capture_scope', repeat('f', 64), '{}'::jsonb),
    ('capture-manifest:' || repeat('1', 64), 'capture_manifest', repeat('1', 64), '{}'::jsonb),
    (
        'capture-evaluation:' || repeat('2', 64),
        'capture_evaluation_report',
        repeat('2', 64),
        '{}'::jsonb
    ),
    ('trace-bundle:' || repeat('3', 64), 'trace_bundle', repeat('3', 64), '{}'::jsonb),
    (
        'strategy-usage-audit:' || repeat('4', 64),
        'strategy_usage_audit',
        repeat('4', 64),
        '{}'::jsonb
    ),
    (
        'usage-frequency:' || repeat('6', 64),
        'usage_frequency_slice',
        repeat('6', 64),
        '{}'::jsonb
    ),
    (
        'strategy-data-quality-review:' || repeat('7', 64),
        'strategy_data_quality_review',
        repeat('7', 64),
        '{}'::jsonb
    ),
    (
        'graduation-attestation:' || repeat('5', 64),
        'graduation_attestation',
        repeat('5', 64),
        '{}'::jsonb
    );

do $$
declare
    durable_kind_count integer;
    kind_tamper_rejected boolean := false;
    hash_tamper_rejected boolean := false;
    unknown_kind_rejected boolean := false;
    update_rejected boolean := false;
    delete_rejected boolean := false;
begin
    select count(distinct contract_kind)
    into durable_kind_count
    from staging.contract_objects
    where contract_id in (
        'registry-snapshot:' || repeat('b', 64),
        'research-catalog:' || repeat('c', 64),
        'snapshot:' || repeat('d', 64),
        'release-manifest:' || repeat('e', 64),
        'capture-scope:' || repeat('f', 64),
        'capture-manifest:' || repeat('1', 64),
        'capture-evaluation:' || repeat('2', 64),
        'trace-bundle:' || repeat('3', 64),
        'strategy-usage-audit:' || repeat('4', 64),
        'usage-frequency:' || repeat('6', 64),
        'strategy-data-quality-review:' || repeat('7', 64),
        'graduation-attestation:' || repeat('5', 64)
    );
    if durable_kind_count <> 12 then
        raise exception 'staging.contract_objects does not accept every durable contract kind';
    end if;

    if to_regclass('staging.idx_contract_objects_usage_audit_run') is null then
        raise exception 'strategy usage audits are not indexed by strategy_run_id';
    end if;
    if to_regclass('staging.idx_contract_objects_quality_review_run') is null then
        raise exception 'strategy quality reviews are not indexed by strategy_run_id';
    end if;

    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'capture-scope:' || repeat('8', 64),
            'capture_manifest',
            repeat('8', 64),
            '{}'::jsonb
        );
    exception when check_violation then
        kind_tamper_rejected := true;
    end;
    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'trace-bundle:' || repeat('9', 64),
            'trace_bundle',
            repeat('a', 64),
            '{}'::jsonb
        );
    exception when check_violation then
        hash_tamper_rejected := true;
    end;
    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'unknown-contract:' || repeat('0', 64),
            'unknown_contract',
            repeat('0', 64),
            '{}'::jsonb
        );
    exception when check_violation then
        unknown_kind_rejected := true;
    end;
    begin
        update staging.contract_objects
        set payload = '{"tampered":true}'::jsonb
        where contract_id = 'graduation-attestation:' || repeat('5', 64);
    exception when raise_exception then
        update_rejected := true;
    end;
    begin
        delete from staging.contract_objects
        where contract_id = 'release-manifest:' || repeat('e', 64);
    exception when raise_exception then
        delete_rejected := true;
    end;
    if not kind_tamper_rejected or not hash_tamper_rejected or not unknown_kind_rejected then
        raise exception 'staging.contract_objects accepted kind or content identity tamper';
    end if;
    if not update_rejected or not delete_rejected then
        raise exception 'staging.contract_objects is not append-only';
    end if;
end;
$$;

rollback;
