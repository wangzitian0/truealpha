begin;

do $$
declare
    default_expression text;
begin
    if to_regclass('raw.fetches') is null then
        raise exception 'raw.fetches is missing';
    end if;
    -- #1730 Slice 2: legacy staging.kg_* tables are retired; verify they do not exist.
    if to_regclass('staging.kg_entities') is not null then
        raise exception 'retired staging.kg_entities must not exist';
    end if;
    if to_regclass('staging.kg_edges') is not null then
        raise exception 'retired staging.kg_edges must not exist';
    end if;
    if to_regclass('staging.kg_identifiers') is not null then
        raise exception 'retired staging.kg_identifiers must not exist';
    end if;
    if to_regclass('staging.entities') is null then
        raise exception 'Postgres canonical entity store (staging.entities) is missing';
    end if;
    if to_regclass('staging.contract_objects') is null then
        raise exception 'immutable Gate 0 contract repository is missing';
    end if;

    -- #1258: staging.financial_facts is retired; verify it does not exist.
    if to_regclass('staging.financial_facts') is not null then
        raise exception 'retired staging.financial_facts must not exist';
    end if;

    select column_default into default_expression
    from information_schema.columns
    where table_schema = 'staging'
      and table_name = 'capture_normalized_observations'
      and column_name = 'knowable_at';
    if not found or default_expression is not null then
        raise exception 'capture_normalized_observations.knowable_at must exist and be explicit';
    end if;

    select column_default into default_expression
    from information_schema.columns
    where table_schema = 'staging'
      and table_name = 'market_prices_daily'
      and column_name = 'transaction_time';
    if not found or default_expression is not null then
        raise exception 'market_prices_daily.transaction_time must exist and be explicit';
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

do $$
declare
    mv_name text;
    mv_kind "char";
    has_unique_idx boolean;
    gov_def text;
    rollback_id bigint;
    delete_rejected boolean := false;
    immutable_rejected boolean := false;
    reconsume_rejected boolean := false;
begin
    foreach mv_name in array array[
        'entity_identity',
        'fund_holdings',
        'fund_holdings_resolved',
        'fund_holdings_coverage',
        'fund_holdings_valuation'
    ] loop
        select c.relkind into mv_kind
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart' and c.relname = mv_name;
        if mv_kind is distinct from 'm' then
            raise exception 'mart.% must be a materialized view, got %', mv_name, mv_kind;
        end if;
        if not has_table_privilege('mart_readonly', 'mart.' || mv_name, 'select') then
            raise exception 'mart_readonly must hold select on mart.%', mv_name;
        end if;
        select exists (
            select 1
            from pg_index x
            join pg_class c on c.oid = x.indrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'mart'
              and c.relname = mv_name
              and x.indisunique
              and x.indisvalid
              and x.indpred is null
        ) into has_unique_idx;
        if not has_unique_idx then
            raise exception 'mart.% lacks a valid full unique index for CONCURRENTLY refresh', mv_name;
        end if;
    end loop;

    select pg_get_viewdef('mart.governed_strategy_run'::regclass, true) into gov_def;
    if position('served_head' in gov_def) = 0
       or position('universe:canary-%' in gov_def) = 0
       or position('run_id is not null' in lower(gov_def)) = 0 then
        raise exception 'mart.governed_strategy_run must read mart.served_head, exclude canary, and require run_id is not null';
    end if;

    insert into ops.head_rollback_requests (
        environment, universe_id, universe_version, factor_id, target_run_id, reason, requested_by, dedupe_key
    ) values (
        'ci', 'universe:topt-us-2026-03-31', 'v1', 'gross_profit_per_employee',
        'capture-run:' || repeat('a', 64), 'contract test rollback', 'principal:admin', 'rollback-contract-1'
    ) returning request_id into rollback_id;

    begin
        delete from ops.head_rollback_requests where request_id = rollback_id;
    exception when check_violation then
        delete_rejected := true;
    end;
    if not delete_rejected then
        raise exception 'ops.head_rollback_requests accepted delete';
    end if;

    begin
        update ops.head_rollback_requests set reason = 'tampered' where request_id = rollback_id;
    exception when check_violation then
        immutable_rejected := true;
    end;
    if not immutable_rejected then
        raise exception 'ops.head_rollback_requests accepted immutable field update';
    end if;

    update ops.head_rollback_requests
    set consumed_at = now(), result_pointer_id = 'current-pointer:' || repeat('b', 64)
    where request_id = rollback_id;

    begin
        update ops.head_rollback_requests
        set consumed_at = now(), result_pointer_id = 'current-pointer:' || repeat('c', 64)
        where request_id = rollback_id;
    exception when check_violation then
        reconsume_rejected := true;
    end;
    if not reconsume_rejected then
        raise exception 'ops.head_rollback_requests accepted second consume update';
    end if;
end;
$$;

rollback;
