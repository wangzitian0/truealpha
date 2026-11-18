-- #1664 S2: retire legacy tables and converge Staging schema drift.

do $$
declare
    v_count bigint;
    v_condef text;
    v_expected_condef constant text := $con$CHECK ((((contract_kind = 'registry_snapshot'::text) AND (contract_id ~~ 'registry-snapshot:%'::text)) OR ((contract_kind = 'research_catalog_manifest'::text) AND (contract_id ~~ 'research-catalog:%'::text)) OR ((contract_kind = 'snapshot_manifest'::text) AND (contract_id ~~ 'snapshot:%'::text)) OR ((contract_kind = 'release_manifest'::text) AND (contract_id ~~ 'release-manifest:%'::text)) OR ((contract_kind = 'capture_scope'::text) AND (contract_id ~~ 'capture-scope:%'::text)) OR ((contract_kind = 'capture_manifest'::text) AND (contract_id ~~ 'capture-manifest:%'::text)) OR ((contract_kind = 'capture_evaluation_report'::text) AND (contract_id ~~ 'capture-evaluation:%'::text)) OR ((contract_kind = 'trace_bundle'::text) AND (contract_id ~~ 'trace-bundle:%'::text)) OR ((contract_kind = 'strategy_usage_audit'::text) AND (contract_id ~~ 'strategy-usage-audit:%'::text)) OR ((contract_kind = 'graduation_attestation'::text) AND (contract_id ~~ 'graduation-attestation:%'::text)) OR ((contract_kind = 'usage_frequency_slice'::text) AND (contract_id ~~ 'usage-frequency:%'::text)) OR ((contract_kind = 'strategy_data_quality_review'::text) AND (contract_id ~~ 'strategy-data-quality-review:%'::text)) OR ((contract_kind = 'concept-mapping'::text) AND (contract_id ~~ 'concept-mapping:%'::text)) OR ((contract_kind ~~ 'universe-list:%'::text) AND (contract_id ~~ 'universe-list:%'::text))))$con$;
    v_default text;
begin
    if to_regclass('app.clarification_tokens') is not null then
        execute 'select count(*) from app.clarification_tokens' into v_count;
        if v_count > 0 then
            raise exception 'app.clarification_tokens still contains % row(s); refusing drop', v_count;
        end if;
        execute 'drop table app.clarification_tokens';
    end if;

    if to_regclass('app.research_gap_requests') is not null then
        execute 'select count(*) from app.research_gap_requests' into v_count;
        if v_count > 0 then
            raise exception 'app.research_gap_requests still contains % row(s); refusing drop', v_count;
        end if;
        execute 'drop table app.research_gap_requests';
    end if;

    if to_regprocedure('app.reject_clarification_token_field_tamper()') is not null then
        execute 'drop function app.reject_clarification_token_field_tamper()';
    end if;

    if to_regprocedure('staging.reject_legacy_market_price_insert()') is not null then
        execute 'drop function staging.reject_legacy_market_price_insert()';
    end if;

    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'staging'
          and table_name = 'analyst_rating_events'
          and column_name in ('action', 'source', 'mapping_version')
    ) then
        execute 'alter table staging.analyst_rating_events
            drop column if exists action,
            drop column if exists source,
            drop column if exists mapping_version';
    end if;

    if to_regclass('staging.uq_fund_holding_line_vintage') is not null
       and pg_get_indexdef('staging.uq_fund_holding_line_vintage'::regclass)
           is distinct from 'CREATE UNIQUE INDEX uq_fund_holding_line_vintage ON staging.fund_holding_facts USING btree (fund_id, holding_name, report_period, transaction_time, COALESCE(isin, ''''::text), COALESCE(cusip, ''''::text))' then
        execute 'drop index staging.uq_fund_holding_line_vintage';
    end if;

    if exists (
        select 1
        from pg_constraint
        where conrelid = 'staging.fund_holding_facts'::regclass
          and conname = 'fund_holding_facts_currency_check'
    ) then
        execute 'alter table staging.fund_holding_facts drop constraint fund_holding_facts_currency_check';
    end if;

    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'staging'
          and table_name = 'fund_holding_facts'
          and column_name in ('instrument_id', 'listing_id', 'asset_type', 'currency', 'mapping_version')
    ) then
        execute 'alter table staging.fund_holding_facts
            drop column if exists instrument_id,
            drop column if exists listing_id,
            drop column if exists asset_type,
            drop column if exists currency,
            drop column if exists mapping_version';
    end if;

    if to_regclass('staging.uq_fund_holding_line_vintage') is null then
        execute 'CREATE UNIQUE INDEX uq_fund_holding_line_vintage ON staging.fund_holding_facts USING btree (fund_id, holding_name, report_period, transaction_time, COALESCE(isin, ''''::text), COALESCE(cusip, ''''::text))';
    end if;

    for v_default in
        select format('%I.%I', n.nspname, c.relname)
        from pg_attrdef d
        join pg_attribute a on a.attrelid = d.adrelid and a.attnum = d.adnum
        join pg_class c on c.oid = d.adrelid
        join pg_namespace n on n.oid = c.relnamespace
        where (n.nspname, c.relname) in (
            ('raw', 'fetches'),
            ('staging', 'analyst_rating_events'),
            ('staging', 'fund_holding_facts')
        )
          and a.attname = 'recorded_at'
          and pg_get_expr(d.adbin, d.adrelid) is distinct from 'now()'
    loop
        execute format('alter table %s alter column recorded_at set default now()', v_default);
    end loop;

    select pg_get_constraintdef(oid)
      into v_condef
    from pg_constraint
    where conrelid = 'staging.contract_objects'::regclass
      and conname = 'contract_objects_kind_identity_check';

    if v_condef is distinct from v_expected_condef then
        execute 'alter table staging.contract_objects drop constraint if exists contract_objects_kind_identity_check';
        execute format(
            'alter table staging.contract_objects add constraint contract_objects_kind_identity_check %s',
            v_expected_condef
        );
    end if;

    select pg_get_expr(d.adbin, d.adrelid)
      into v_default
    from pg_attrdef d
    join pg_attribute a on a.attrelid = d.adrelid and a.attnum = d.adnum
    where d.adrelid = 'mart.issuer_supply_chain_exposure'::regclass
      and a.attname = 'extractor';

    if v_default is distinct from '''graph:entity-relations:v1''::text' then
        execute 'alter table mart.issuer_supply_chain_exposure alter column extractor set default ''graph:entity-relations:v1''::text';
    end if;
end $$;
