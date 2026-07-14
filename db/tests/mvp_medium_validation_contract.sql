begin;

do $$
declare
    required_table_count integer;
    required_column_count integer;
    required_trigger_count integer;
begin
    select count(*) into required_table_count
    from (values
        ('mvp_market_prices'),
        ('mvp_financial_facts'),
        ('mvp_corporate_actions'),
        ('mvp_universe_memberships'),
        ('mvp_issuer_security_links'),
        ('mvp_security_listing_links')
    ) as required(table_name)
    where to_regclass('staging.' || required.table_name) is not null;
    if required_table_count <> 6 then
        raise exception 'MVP medium typed projections are incomplete';
    end if;

    select count(*) into required_column_count
    from information_schema.columns
    where table_schema = 'staging'
      and table_name in (
          'mvp_market_prices',
          'mvp_financial_facts',
          'mvp_corporate_actions',
          'mvp_universe_memberships',
          'mvp_issuer_security_links',
          'mvp_security_listing_links'
      )
      and column_name in ('normalized_record_id', 'confidence', 'raw_ref')
      and is_nullable = 'NO';
    if required_column_count <> 18 then
        raise exception 'MVP medium identity, confidence, or raw lineage columns are incomplete';
    end if;

    select count(*) into required_trigger_count
    from pg_trigger
    where not tgisinternal
      and (
          tgname = 'trg_market_prices_reject_insert'
          or tgname like 'trg_mvp_%_validate'
          or tgname like 'trg_mvp_%_append_only'
      );
    if required_trigger_count <> 13 then
        raise exception 'MVP medium sealing, validation, or append-only triggers are incomplete';
    end if;
    if to_regclass('staging.uq_normalized_records_single_successor') is null then
        raise exception 'normalized restatements lack the single-successor constraint';
    end if;
end;
$$;

do $$
declare
    fetch_id bigint;
    raw_reference text;
    before_count integer;
    after_count integer;
    after_id text;
begin
    insert into raw.fetches (
        source, source_record_id, payload_sha256, object_uri, content_type,
        byte_length, fetched_at, recorded_at, metadata
    ) values (
        'sec', 'd2-e2-sql-contract', repeat('a', 64),
        's3://d2-e2-contract/raw-object', 'application/json', 2,
        '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', '{}'
    ) returning id into fetch_id;
    raw_reference := 'raw.fetches:' || fetch_id;

    insert into staging.normalized_records (
        normalized_record_id, content_sha256, semantic_type_id,
        semantic_type_version, subject_kind, subject_id, valid_time,
        transaction_time, recorded_at, confidence, document_id,
        raw_object_id, raw_object_sha256, raw_ref,
        source_registry_entry_id, source_registry_entry_sha256,
        mapping_version, mapping_implementation_sha256,
        payload_model_key, payload_schema_sha256, payload_sha256,
        payload, record_ref, is_restatement, supersedes_record_id
    ) values (
        'normalized-record:' || repeat('d', 64), repeat('d', 64),
        'semantic.market-price', '1.0.0', 'listing', 'listing:test',
        daterange('2026-03-31', '2026-03-31', '[]'),
        '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99,
        'price:test:2026-03-31', 'raw-object:' || repeat('a', 64),
        repeat('a', 64), raw_reference,
        'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
        'fixture:1.0.0', repeat('c', 64), 'test:MarketPrice',
        repeat('c', 64), repeat('c', 64), '{}', '{}', false, null
    );

    insert into staging.mvp_market_prices (
        normalized_record_id, subject_kind, subject_id, input_id, issuer_id,
        security_id, listing_id, share_class, exchange_mic, ticker, calendar_id,
        calendar_version, trading_date, session_close_at, open, high, low, close,
        volume, currency, price_basis, confidence_policy_id, price_policy_id,
        valid_time, transaction_time, recorded_at, confidence, raw_ref
    ) values (
        'normalized-record:' || repeat('d', 64), 'listing', 'listing:test',
        'price:test:2026-03-31', 'issuer:test', 'security:test', 'listing:test',
        'common', 'XNAS', 'TEST', 'calendar:test', '1.0.0', '2026-03-31',
        '2026-03-31T20:00:00Z', 10, 12, 9, 11, 100, 'USD', 'unadjusted',
        'policy:confidence', 'policy:price',
        daterange('2026-03-31', '2026-03-31', '[]'),
        '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99, raw_reference
    );

    begin
        update staging.mvp_market_prices
        set close = 10
        where normalized_record_id = 'normalized-record:' || repeat('d', 64);
        raise exception 'MVP market-price UPDATE unexpectedly succeeded';
    exception when others then
        if sqlerrm not like 'point-in-time records are append-only%' then
            raise;
        end if;
    end;
    begin
        delete from staging.mvp_market_prices
        where normalized_record_id = 'normalized-record:' || repeat('d', 64);
        raise exception 'MVP market-price DELETE unexpectedly succeeded';
    exception when others then
        if sqlerrm not like 'point-in-time records are append-only%' then
            raise;
        end if;
    end;
    begin
        insert into staging.market_prices default values;
        raise exception 'legacy market-price INSERT unexpectedly succeeded';
    exception when others then
        if sqlerrm not like 'legacy staging.market_prices has no confidence contract%' then
            raise;
        end if;
    end;

    insert into staging.normalized_records (
        normalized_record_id, content_sha256, semantic_type_id,
        semantic_type_version, subject_kind, subject_id, valid_time,
        transaction_time, recorded_at, confidence, document_id,
        raw_object_id, raw_object_sha256, raw_ref,
        source_registry_entry_id, source_registry_entry_sha256,
        mapping_version, mapping_implementation_sha256,
        payload_model_key, payload_schema_sha256, payload_sha256,
        payload, record_ref, is_restatement, supersedes_record_id
    ) values (
        'normalized-record:' || repeat('e', 64), repeat('e', 64),
        'semantic.market-price', '1.0.0', 'listing', 'listing:test',
        daterange('2026-04-01', '2026-04-01', '[]'),
        '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99,
        'price:test:2026-04-01', 'raw-object:' || repeat('a', 64),
        repeat('a', 64), raw_reference,
        'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
        'fixture:1.0.0', repeat('c', 64), 'test:MarketPrice',
        repeat('c', 64), repeat('c', 64), '{}', '{}', false, null
    );
    begin
        insert into staging.mvp_market_prices (
            normalized_record_id, subject_kind, subject_id, input_id, issuer_id,
            security_id, listing_id, share_class, exchange_mic, ticker, calendar_id,
            calendar_version, trading_date, session_close_at, open, high, low, close,
            volume, currency, price_basis, confidence_policy_id, price_policy_id,
            valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            'normalized-record:' || repeat('e', 64), 'listing', 'listing:test',
            'price:test:2026-04-01', 'issuer:test', 'security:test', 'listing:test',
            'common', 'XNAS', 'TEST', 'calendar:test', '1.0.0', '2026-04-01',
            '2026-04-01T20:00:00Z', 10, 12, 9, 11, 100, 'USD', 'adjusted',
            'policy:confidence', 'policy:price',
            daterange('2026-04-01', '2026-04-01', '[]'),
            '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99, raw_reference
        );
        raise exception 'adjusted executable market price unexpectedly succeeded';
    exception when check_violation then
        null;
    end;

    insert into staging.normalized_records (
        normalized_record_id, content_sha256, semantic_type_id,
        semantic_type_version, subject_kind, subject_id, valid_time,
        transaction_time, recorded_at, confidence, document_id,
        raw_object_id, raw_object_sha256, raw_ref,
        source_registry_entry_id, source_registry_entry_sha256,
        mapping_version, mapping_implementation_sha256,
        payload_model_key, payload_schema_sha256, payload_sha256,
        payload, record_ref, is_restatement, supersedes_record_id
    ) values (
        'normalized-record:' || repeat('f', 64), repeat('f', 64),
        'semantic.financial-fact', '1.0.0', 'listing', 'listing:test',
        daterange('2026-04-02', '2026-04-02', '[]'),
        '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99,
        'wrong-type:test', 'raw-object:' || repeat('a', 64),
        repeat('a', 64), raw_reference,
        'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
        'fixture:1.0.0', repeat('c', 64), 'test:FinancialFact',
        repeat('c', 64), repeat('c', 64), '{}', '{}', false, null
    );
    begin
        insert into staging.mvp_market_prices (
            normalized_record_id, subject_kind, subject_id, input_id, issuer_id,
            security_id, listing_id, share_class, exchange_mic, ticker, calendar_id,
            calendar_version, trading_date, session_close_at, open, high, low, close,
            volume, currency, price_basis, confidence_policy_id, price_policy_id,
            valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            'normalized-record:' || repeat('f', 64), 'listing', 'listing:test',
            'price:test:2026-04-02', 'issuer:test', 'security:test', 'listing:test',
            'common', 'XNAS', 'TEST', 'calendar:test', '1.0.0', '2026-04-02',
            '2026-04-02T20:00:00Z', 10, 12, 9, 11, 100, 'USD', 'unadjusted',
            'policy:confidence', 'policy:price',
            daterange('2026-04-02', '2026-04-02', '[]'),
            '2026-07-12T00:00:00Z', '2026-07-12T00:00:01Z', 0.99, raw_reference
        );
        raise exception 'wrong semantic projection unexpectedly succeeded';
    exception when check_violation then
        if sqlerrm not like 'MVP projection does not match normalized record%' then
            raise;
        end if;
    end;

    insert into staging.normalized_records (
        normalized_record_id, content_sha256, semantic_type_id,
        semantic_type_version, subject_kind, subject_id, valid_time,
        transaction_time, recorded_at, confidence, document_id,
        raw_object_id, raw_object_sha256, raw_ref,
        source_registry_entry_id, source_registry_entry_sha256,
        mapping_version, mapping_implementation_sha256,
        payload_model_key, payload_schema_sha256, payload_sha256,
        payload, record_ref, is_restatement, supersedes_record_id
    ) values (
        'normalized-record:' || repeat('2', 64), repeat('2', 64),
        'semantic.financial-fact', '1.0.0', 'issuer', 'issuer:test',
        daterange('2020-01-01', '2020-12-31', '[]'),
        '2021-01-01T00:00:00Z', '2021-01-01T00:00:01Z', 0.99,
        'fact:test:original', 'raw-object:' || repeat('a', 64),
        repeat('a', 64), raw_reference,
        'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
        'fixture:1.0.0', repeat('c', 64), 'test:FinancialFact',
        repeat('c', 64), repeat('c', 64), '{}', '{}', false, null
    );
    insert into staging.normalized_records (
        normalized_record_id, content_sha256, semantic_type_id,
        semantic_type_version, subject_kind, subject_id, valid_time,
        transaction_time, recorded_at, confidence, document_id,
        raw_object_id, raw_object_sha256, raw_ref,
        source_registry_entry_id, source_registry_entry_sha256,
        mapping_version, mapping_implementation_sha256,
        payload_model_key, payload_schema_sha256, payload_sha256,
        payload, record_ref, is_restatement, supersedes_record_id
    ) values (
        'normalized-record:' || repeat('3', 64), repeat('3', 64),
        'semantic.financial-fact', '1.0.0', 'issuer', 'issuer:test',
        daterange('2020-01-01', '2020-12-31', '[]'),
        '2022-01-01T00:00:00Z', '2022-01-01T00:00:01Z', 0.99,
        'fact:test:amended', 'raw-object:' || repeat('a', 64),
        repeat('a', 64), raw_reference,
        'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
        'fixture:1.0.0', repeat('c', 64), 'test:FinancialFact',
        repeat('c', 64), repeat('c', 64), '{}', '{}', true,
        'normalized-record:' || repeat('2', 64)
    );

    select count(*) into before_count
    from staging.normalized_records candidate
    where candidate.subject_id = 'issuer:test'
      and candidate.semantic_type_id = 'semantic.financial-fact'
      and candidate.transaction_time <= '2021-06-01T00:00:00Z'
      and not exists (
          select 1 from staging.normalized_records replacement
          where replacement.supersedes_record_id = candidate.normalized_record_id
            and replacement.transaction_time <= '2021-06-01T00:00:00Z'
      );
    select count(*), max(normalized_record_id) into after_count, after_id
    from staging.normalized_records candidate
    where candidate.subject_id = 'issuer:test'
      and candidate.semantic_type_id = 'semantic.financial-fact'
      and candidate.transaction_time <= '2022-06-01T00:00:00Z'
      and not exists (
          select 1 from staging.normalized_records replacement
          where replacement.supersedes_record_id = candidate.normalized_record_id
            and replacement.transaction_time <= '2022-06-01T00:00:00Z'
      );
    if before_count <> 1
       or after_count <> 1
       or after_id <> 'normalized-record:' || repeat('3', 64) then
        raise exception 'normalized PIT restatement selection is not stable';
    end if;

    begin
        insert into staging.normalized_records (
            normalized_record_id, content_sha256, semantic_type_id,
            semantic_type_version, subject_kind, subject_id, valid_time,
            transaction_time, recorded_at, confidence, document_id,
            raw_object_id, raw_object_sha256, raw_ref,
            source_registry_entry_id, source_registry_entry_sha256,
            mapping_version, mapping_implementation_sha256,
            payload_model_key, payload_schema_sha256, payload_sha256,
            payload, record_ref, is_restatement, supersedes_record_id
        ) values (
            'normalized-record:' || repeat('4', 64), repeat('4', 64),
            'semantic.financial-fact', '1.0.0', 'issuer', 'issuer:test',
            daterange('2020-01-01', '2020-12-31', '[]'),
            '2023-01-01T00:00:00Z', '2023-01-01T00:00:01Z', 0.99,
            'fact:test:competing', 'raw-object:' || repeat('a', 64),
            repeat('a', 64), raw_reference,
            'source-registry-entry:' || repeat('b', 64), repeat('b', 64),
            'fixture:1.0.0', repeat('c', 64), 'test:FinancialFact',
            repeat('c', 64), repeat('c', 64), '{}', '{}', true,
            'normalized-record:' || repeat('2', 64)
        );
        raise exception 'competing normalized successor unexpectedly succeeded';
    exception when unique_violation then
        null;
    end;
end;
$$;

rollback;
