-- idempotent: true
-- #1851: compute mart.issuer_basic_info market metrics via set-based CTEs instead of per-row lateral scans (5.3s -> 37ms).
-- Guard DDL with unique 'mkt_by_issuer' check so replays under boot lock_timeout take zero exclusive locks.

do $$
declare
  rel oid := to_regclass('mart.issuer_basic_info');
begin
  if rel is null or position('mkt_by_issuer' in lower(pg_get_viewdef(rel, true))) = 0 then
    execute $vc$create or replace view mart.issuer_basic_info as
      with active_heads as materialized (
        select h.run_id, h.advanced_at
        from mart.served_head h
        where h.run_id is not null
          and not starts_with(h.universe_id, 'universe:canary-')
      ),
      latest_nodes as (
        select distinct on (m.issuer_id, m.node_key)
               m.issuer_id,
               m.node_key,
               m.value,
               m.confidence,
               m.period
        from mart.metric_node_values m
        join active_heads h on h.run_id = m.run_id
        where m.node_key in ('market_cap', 'shares_outstanding', 'price')
        order by m.issuer_id, m.node_key, h.advanced_at desc, m.period desc, m.run_id desc
      ),
      mkt_by_issuer as (
        select issuer_id,
               max(case when node_key = 'market_cap' then value end) as market_cap,
               max(case when node_key = 'market_cap' then confidence end) as market_cap_confidence,
               max(case when node_key = 'shares_outstanding' then value end) as shares_outstanding,
               max(case when node_key = 'price' then value end) as close_price,
               max(period) as as_of_cutoff
        from latest_nodes
        group by issuer_id
      )
      select distinct on (
        coalesce(
          lpad(p.cik::text, 10, '0'),
          nullif(ei.cik, ''),
          nullif(upper(btrim(ei.current_ticker)), ''),
          ei.entity_id::text
        )
      )
             ei.entity_id::text as issuer_id,
             ei.name as canonical_name,
             coalesce(
               case
                 when split_part(ei.listing_id, ':', 2) in ('xnas', 'xnys', 'arcx', 'bats')
                   then nullif(upper(split_part(ei.listing_id, ':', 3)), '')
               end,
               ei.current_ticker
             ) as primary_ticker,
             coalesce(p.primary_exchange, nullif(split_part(ei.listing_id, ':', 2), '')) as primary_exchange,
             coalesce(lpad(p.cik::text, 10, '0'), ei.cik) as cik,
             p.sic,
             p.sic_description,
             coalesce(p.sic_description, p.category) as sector_gics_or_sic,
             p.fiscal_year_end,
             p.state_of_incorporation,
             p.entity_type,
             coalesce(mkt_e.market_cap, mkt_l.market_cap) as market_cap,
             coalesce(mkt_e.market_cap_confidence, mkt_l.market_cap_confidence) as market_cap_confidence,
             coalesce(mkt_e.shares_outstanding, mkt_l.shares_outstanding) as shares_outstanding,
             coalesce(mkt_e.close_price, mkt_l.close_price) as close_price,
             coalesce(mkt_e.as_of_cutoff, mkt_l.as_of_cutoff) as as_of_cutoff
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
      left join mkt_by_issuer mkt_e on mkt_e.issuer_id = ei.entity_id::text
      left join mkt_by_issuer mkt_l on ei.legacy_id is not null and mkt_l.issuer_id = ei.legacy_id
      where ei.kind = 'issuer'
      order by
        coalesce(
          lpad(p.cik::text, 10, '0'),
          nullif(ei.cik, ''),
          nullif(upper(btrim(ei.current_ticker)), ''),
          ei.entity_id::text
        ),
        (coalesce(mkt_e.market_cap, mkt_l.market_cap) is not null) desc,
        (p.exact_match is true) desc,
        (p.sic is not null) desc,
        (coalesce(p.primary_exchange, nullif(split_part(ei.listing_id, ':', 2), '')) ~ '^[A-Z]+$') desc,
        (ei.current_ticker is not null) desc,
        ei.entity_id::text$vc$;
  end if;
end $$;
