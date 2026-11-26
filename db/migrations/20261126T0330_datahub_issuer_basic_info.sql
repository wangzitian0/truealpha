-- idempotent: true
-- #1851: issuer basic profile metadata in staging and read-only mart.issuer_basic_info view.

do $$ begin
  if to_regclass('staging.issuer_profiles') is null then
    create table staging.issuer_profiles (
      issuer_id uuid not null,
      cik integer not null,
      sic text,
      sic_description text,
      category text,
      state_of_incorporation text,
      fiscal_year_end text,
      primary_exchange text,
      entity_type text,
      source_fetch_id bigint,
      knowable_at timestamp with time zone not null,
      recorded_at timestamp with time zone default clock_timestamp() not null,
      constraint issuer_profiles_pkey PRIMARY KEY (issuer_id),
      constraint issuer_profiles_cik_check CHECK ((cik > 0))
    );
  end if;
end $$;

do $$ begin
  if to_regclass('staging.ix_issuer_profiles_cik') is null then
    create index ix_issuer_profiles_cik on staging.issuer_profiles using btree (cik);
  end if;
end $$;

do $$ begin
  if to_regclass('mart.issuer_basic_info') is null then
    execute $vc$create view mart.issuer_basic_info as
      select ei.entity_id::text as issuer_id,
             ei.name as canonical_name,
             ei.current_ticker as primary_ticker,
             coalesce(p.primary_exchange, nullif(split_part(ei.listing_id, ':', 2), '')) as primary_exchange,
             coalesce(lpad(p.cik::text, 10, '0'), ei.cik) as cik,
             p.sic,
             p.sic_description,
             coalesce(p.sic_description, p.category) as sector_gics_or_sic,
             p.fiscal_year_end,
             p.state_of_incorporation,
             p.entity_type,
             mkt.market_cap,
             mkt.market_cap_confidence,
             mkt.shares_outstanding,
             mkt.close_price,
             mkt.as_of_cutoff
      from mart.entity_identity ei
      left join staging.issuer_profiles p on p.issuer_id = ei.entity_id
      left join lateral (
        with latest_per_node as (
          select distinct on (m.node_key)
                 m.node_key, m.value, m.confidence, m.period
          from mart.metric_node_values m
          join mart.served_head h on h.run_id = m.run_id
          where (m.issuer_id = ei.entity_id::text or (ei.legacy_id is not null and m.issuer_id = ei.legacy_id))
            and h.run_id is not null
            and not starts_with(h.universe_id, 'universe:canary-')
            and m.node_key in ('market_cap', 'shares_outstanding', 'price')
          order by m.node_key, h.advanced_at desc, m.period desc, m.run_id desc
        )
        select max(case when node_key = 'market_cap' then value end) as market_cap,
               max(case when node_key = 'market_cap' then confidence end) as market_cap_confidence,
               max(case when node_key = 'shares_outstanding' then value end) as shares_outstanding,
               max(case when node_key = 'price' then value end) as close_price,
               max(period) as as_of_cutoff
        from latest_per_node
      ) mkt on true
      where ei.kind = 'issuer'$vc$;
  end if;
end $$;
