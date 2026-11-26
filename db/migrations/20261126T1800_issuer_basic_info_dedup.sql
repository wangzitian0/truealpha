-- idempotent: true
-- #1851: deduplicate issuer entities and join SEC profiles by entity_id or CIK in mart.issuer_basic_info.

create or replace view mart.issuer_basic_info as
  select distinct on (
    coalesce(
      nullif(upper(btrim(ei.current_ticker)), ''),
      coalesce(lpad(p.cik::text, 10, '0'), nullif(ei.cik, '')),
      ei.entity_id::text
    )
  )
         ei.entity_id::text as issuer_id,
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
  left join lateral (
    select p.cik,
           p.sic,
           p.sic_description,
           p.category,
           p.fiscal_year_end,
           p.state_of_incorporation,
           p.entity_type,
           p.primary_exchange,
           (p.issuer_id = ei.entity_id) as exact_match
    from staging.issuer_profiles p
    where p.issuer_id = ei.entity_id
       or (ei.cik ~ '^[0-9]+$' and p.cik = ei.cik::integer)
    order by (p.issuer_id = ei.entity_id) desc, p.knowable_at desc, p.recorded_at desc
    limit 1
  ) p on true
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
  where ei.kind = 'issuer'
  order by
    coalesce(
      nullif(upper(btrim(ei.current_ticker)), ''),
      coalesce(lpad(p.cik::text, 10, '0'), nullif(ei.cik, '')),
      ei.entity_id::text
    ),
    (mkt.market_cap is not null) desc,
    (p.exact_match is true) desc,
    (p.sic is not null) desc,
    (coalesce(p.primary_exchange, nullif(split_part(ei.listing_id, ':', 2), '')) ~ '^[A-Z]+$') desc,
    (ei.current_ticker is not null) desc,
    ei.entity_id::text;
