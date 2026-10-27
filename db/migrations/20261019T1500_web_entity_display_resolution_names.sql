-- #1358: resolve entity display names from KG legacy-id entries, name aliases, and ETF constituent facts.
--
-- Problem: mart.entity_display_resolution only joined staging.kg_entities on e.id = lm.issuer_id.
-- When staging.topt_core_snapshot_members stores UUID issuer_ids while staging.kg_entities keys
-- entities by legacy_id, or when issuer names live in staging.entity_aliases (scheme = 'name') or
-- staging.etf_constituent_facts (company_name), display_name resolved to NULL and the Web UI
-- rendered bare tickers without company names.
--
-- Superseded: 20261027T1000_datahub_primary_listing_relation.sql redefines the view later in the
-- chain (#1518) and owns its definition. Replacing it here would take ACCESS EXCLUSIVE on the view on
-- every replay, only for that file to replace it again. So this file creates the view only on a
-- database that has none.

do $$
declare
    wanted constant text := $view$
with entity_tickers as (
    select distinct on (entity_id)
        entity_id,
        case
            when scheme = 'mic-ticker' then upper(split_part(value, ':', 2))
            when scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then upper(split_part(value, ':', 3))
        end as ticker
    from staging.entity_aliases
    where (scheme = 'mic-ticker' and nullif(btrim(split_part(value, ':', 2)), '') is not null)
       or (scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$')
    order by entity_id, (scheme = 'mic-ticker') desc, confidence desc, transaction_time desc, alias_id desc
),
latest_members as (
    select distinct on (m.issuer_id)
        m.issuer_id,
        m.listing_id,
        case when m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             then m.listing_id::uuid else null end as listing_uuid,
        case when m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             then m.issuer_id::uuid else null end as issuer_uuid
    from staging.topt_core_snapshot_members m
    left join entity_tickers lt
      on lt.entity_id = (
          case when m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               then m.listing_id::uuid else null end
      )
    order by m.issuer_id,
             case
                 when coalesce(
                     lt.ticker,
                     case
                         when m.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
                         then upper(split_part(m.listing_id, ':', 3))
                     end
                 ) = 'GOOGL' then 0
                 when coalesce(
                     lt.ticker,
                     case
                         when m.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
                         then upper(split_part(m.listing_id, ':', 3))
                     end
                 ) = 'GOOG' then 2
                 else 1
             end,
             m.created_at desc,
             m.listing_id asc
),
issuer_names as (
    select distinct on (entity_id)
        entity_id,
        nullif(btrim(value), '') as display_name
    from staging.entity_aliases
    where scheme = 'name'
      and nullif(btrim(value), '') is not null
    order by entity_id, confidence desc, transaction_time desc, alias_id desc
),
issuer_legacy_ids as (
    select distinct on (entity_id)
        entity_id,
        btrim(value) as legacy_id
    from staging.entity_aliases
    where scheme = 'legacy-id'
      and nullif(btrim(value), '') is not null
    order by entity_id, confidence desc, transaction_time desc, alias_id desc
),
constituent_names as (
    select distinct on (upper(cf.ticker))
        upper(cf.ticker) as ticker,
        btrim(cf.company_name) as display_name
    from staging.etf_constituent_facts cf
    where nullif(btrim(cf.company_name), '') is not null
      and upper(btrim(cf.company_name)) <> upper(btrim(cf.ticker))
    order by upper(cf.ticker), cf.knowable_at desc, cf.as_of desc, cf.id desc
),
resolved_tickers as (
    select
        lm.issuer_id,
        lm.listing_id,
        lm.issuer_uuid,
        coalesce(
            lt.ticker,
            it.ticker,
            case
                when lm.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
                then upper(split_part(lm.listing_id, ':', 3))
                else null
            end
        ) as ticker
    from latest_members lm
    left join entity_tickers lt on lt.entity_id = lm.listing_uuid
    left join entity_tickers it on it.entity_id = lm.issuer_uuid
)
select
    rt.issuer_id,
    rt.listing_id,
    rt.ticker,
    coalesce(
        nullif(btrim(e.display_name), ''),
        nullif(btrim(e_legacy.display_name), ''),
        inam.display_name,
        cn.display_name
    ) as display_name
from resolved_tickers rt
left join issuer_legacy_ids il on il.entity_id = rt.issuer_uuid
left join staging.kg_entities e on e.id = rt.issuer_id
left join staging.kg_entities e_legacy on e_legacy.id = il.legacy_id
left join issuer_names inam on inam.entity_id = rt.issuer_uuid
left join constituent_names cn on cn.ticker = upper(rt.ticker)
order by rt.issuer_id;
$view$;
begin
    if to_regclass('mart.entity_display_resolution') is null then
        execute 'create or replace view mart.entity_display_resolution as ' || wanted;
    end if;
end
$$;

comment on view mart.entity_display_resolution is
    '#987, #1358: issuer -> ticker/display_name for consumer rendering with entity_aliases, kg_entities, and etf_constituent_facts resolution.';
