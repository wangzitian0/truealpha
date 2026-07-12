begin;

do $$
declare
    default_expression text;
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
        'graduation-attestation:' || repeat('5', 64)
    );
    if durable_kind_count <> 10 then
        raise exception 'staging.contract_objects does not accept every durable contract kind';
    end if;

    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'capture-scope:' || repeat('6', 64),
            'capture_manifest',
            repeat('6', 64),
            '{}'::jsonb
        );
    exception when check_violation then
        kind_tamper_rejected := true;
    end;
    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'trace-bundle:' || repeat('7', 64),
            'trace_bundle',
            repeat('8', 64),
            '{}'::jsonb
        );
    exception when check_violation then
        hash_tamper_rejected := true;
    end;
    begin
        insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
        values (
            'unknown-contract:' || repeat('9', 64),
            'unknown_contract',
            repeat('9', 64),
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
