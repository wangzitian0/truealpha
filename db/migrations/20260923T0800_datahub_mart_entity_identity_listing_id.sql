-- #954: expose symbolic listing_id on mart.entity_identity and point mart.entity_display_resolution at mart.entity_identity.

do $$
declare
    wanted constant text := $view$
with active_aliases as (
    select
        staging.entity_survivor(alias.entity_id, 'infinity') as survivor_id,
        alias.scheme,
        alias.value,
        row_number() over (
            partition by staging.entity_survivor(alias.entity_id, 'infinity'), alias.scheme
            order by alias.confidence desc, alias.transaction_time desc
        ) as rn
    from staging.entity_aliases alias
    where staging.entity_alias_valid_to(alias.alias_id, 'infinity') is null
       or staging.entity_alias_valid_to(alias.alias_id, 'infinity') > alias.valid_from
),
latest_by_scheme as (
    select survivor_id, scheme, value
    from active_aliases
    where rn = 1
),
direct_ciks as (select survivor_id, value as cik from latest_by_scheme where scheme = 'cik'),
direct_leis as (select survivor_id, value as lei from latest_by_scheme where scheme = 'lei'),
direct_tickers as (
    select distinct on (survivor_id)
        survivor_id,
        case
            when scheme = 'mic-ticker' then split_part(value, ':', 2)
            when scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then upper(split_part(value, ':', 3))
        end as ticker
    from latest_by_scheme
    where scheme = 'mic-ticker'
       or (scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$')
    order by survivor_id, (scheme = 'mic-ticker') desc
),
issuer_tickers as (
    select distinct on (r_issues.from_entity_id)
        r_issues.from_entity_id as issuer_id,
        dt.ticker
    from staging.entity_relations r_issues
    join staging.entity_relations r_listed
      on r_listed.from_entity_id = r_issues.to_entity_id
     and r_listed.relation_type = 'listed_as'
    join direct_tickers dt
      on dt.survivor_id = r_listed.to_entity_id
    where r_issues.relation_type = 'issues'
      and dt.ticker is not null
    order by r_issues.from_entity_id, r_issues.transaction_time desc
),
instrument_tickers as (
    select distinct on (r_listed.from_entity_id)
        r_listed.from_entity_id as instrument_id,
        dt.ticker
    from staging.entity_relations r_listed
    join direct_tickers dt
      on dt.survivor_id = r_listed.to_entity_id
    where r_listed.relation_type = 'listed_as'
      and dt.ticker is not null
    order by r_listed.from_entity_id, r_listed.transaction_time desc
),
direct_names as (select survivor_id, value as name from latest_by_scheme where scheme = 'name'),
legacy_aliases as (
    select distinct on (survivor_id) survivor_id, value as legacy_id
    from latest_by_scheme
    where scheme = 'legacy-id'
    order by survivor_id
),
direct_listings as (
    select distinct on (survivor_id)
        survivor_id,
        case
            when scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then value
            when scheme = 'mic-ticker'
            then 'listing:' || lower(split_part(value, ':', 1)) || ':' || lower(split_part(value, ':', 2))
        end as listing_id
    from latest_by_scheme
    where scheme = 'legacy-id' or scheme = 'mic-ticker'
    order by survivor_id, (scheme = 'legacy-id') desc
),
issuer_listings as (
    select distinct on (r_issues.from_entity_id)
        r_issues.from_entity_id as issuer_id,
        dl.listing_id
    from staging.entity_relations r_issues
    join staging.entity_relations r_listed
      on r_listed.from_entity_id = r_issues.to_entity_id
     and r_listed.relation_type = 'listed_as'
    join direct_listings dl
      on dl.survivor_id = r_listed.to_entity_id
    where r_issues.relation_type = 'issues'
      and dl.listing_id is not null
    order by r_issues.from_entity_id, r_issues.transaction_time desc
)
select
    e.entity_id,
    e.kind,
    coalesce(dt.ticker, it.ticker, inst.ticker) as current_ticker,
    coalesce(dn.name, kg.display_name) as name,
    dc.cik,
    dl.lei,
    coalesce(dlist.listing_id, ilist.listing_id) as listing_id
from staging.entities e
cross join lateral (select staging.entity_survivor(e.entity_id, 'infinity') as id) survivor
left join direct_tickers dt on dt.survivor_id = survivor.id
left join issuer_tickers it on it.issuer_id = survivor.id
left join instrument_tickers inst on inst.instrument_id = survivor.id
left join direct_names dn on dn.survivor_id = survivor.id
left join legacy_aliases la on la.survivor_id = survivor.id
left join direct_ciks dc on dc.survivor_id = survivor.id
left join direct_leis dl on dl.survivor_id = survivor.id
left join direct_listings dlist on dlist.survivor_id = survivor.id
left join issuer_listings ilist on ilist.issuer_id = survivor.id
left join staging.kg_entities kg on (kg.id = e.entity_id::text or (la.legacy_id is not null and kg.id = la.legacy_id))
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.entity_identity') is null
       or pg_get_viewdef(to_regclass('mart.entity_identity'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.entity_identity as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.entity_identity is
    '#954: canonical entity identity projection for mart consumers (entity_id, kind, current_ticker, name, cik, lei, listing_id).';

do $$
declare
    wanted constant text := $view$
select distinct on (e.entity_id)
    e.entity_id::text as issuer_id,
    e.listing_id,
    e.current_ticker as ticker,
    e.name as display_name
from mart.entity_identity e
where e.kind = 'issuer'
order by e.entity_id
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.entity_display_resolution') is null
       or pg_get_viewdef(to_regclass('mart.entity_display_resolution'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.entity_display_resolution as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.entity_display_resolution is
    '#954: issuer -> ticker/display_name/listing_id for consumer rendering, backed by mart.entity_identity.';
