-- #1641: enforce 1:1 mart.entity_identity cardinality and ETF weight conservation.
--
-- Problem: mart.entity_identity joined staging.kg_entities with an unbounded OR predicate
-- on entity_id::text and legacy_id. Entities with both rows emitted duplicate identity rows.
-- Downstream joins in fund_consolidation multiplied holding weights above 100 percent.
--
-- Rule: mart.entity_identity selects at most one staging.kg_entities row per entity via
-- LATERAL LIMIT 1. mart.fund_virtual_company enforces 0 <= valued <= resolved <= total <= 105.

do $$
declare
    wanted constant text := $view$
with recursive merge_edges as (
    select distinct on (relation.from_entity_id)
        relation.from_entity_id,
        relation.to_entity_id
    from staging.entity_relations relation
    where relation.relation_type = 'superseded_by'
      and not exists (
          select 1 from staging.entity_retractions retraction
          where retraction.relation_id = relation.relation_id)
    order by relation.from_entity_id, relation.transaction_time desc, relation.relation_id desc
),
survivor_walk(entity_id, current_id, depth) as (
    select entity.entity_id, entity.entity_id, 0
    from staging.entities entity
    union all
    select walk.entity_id, edge.to_entity_id, walk.depth + 1
    from survivor_walk walk
    join merge_edges edge on edge.from_entity_id = walk.current_id
    where walk.depth < 32
),
survivors as (
    select distinct on (walk.entity_id) walk.entity_id, walk.current_id as id
    from survivor_walk walk
    order by walk.entity_id, walk.depth desc
),
alias_retractions as (
    select retraction.alias_id, min(retraction.valid_to) as valid_to
    from staging.entity_retractions retraction
    where retraction.alias_id is not null
    group by retraction.alias_id
),
active_aliases as (
    select
        survivor.id as survivor_id,
        alias.scheme,
        alias.value,
        row_number() over (
            partition by survivor.id, alias.scheme
            order by alias.confidence desc, alias.transaction_time desc, alias.alias_id desc
        ) as rn
    from staging.entity_aliases alias
    join survivors survivor on survivor.entity_id = alias.entity_id
    left join alias_retractions retracted on retracted.alias_id = alias.alias_id
    where least(alias.valid_to, retracted.valid_to) is null
       or least(alias.valid_to, retracted.valid_to) > alias.valid_from
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
primary_listings as (
    select distinct on (relation.from_entity_id)
        relation.from_entity_id as issuer_id,
        relation.to_entity_id as listing_id
    from staging.entity_relations relation
    where relation.relation_type = 'primary_listing'
      and not exists (
          select 1 from staging.entity_retractions retraction
          where retraction.relation_id = relation.relation_id)
    order by relation.from_entity_id, relation.transaction_time desc, relation.relation_id desc
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
    left join primary_listings primary_listing
      on primary_listing.issuer_id = r_issues.from_entity_id
     and primary_listing.listing_id = r_listed.to_entity_id
    where r_issues.relation_type = 'issues'
      and dt.ticker is not null
    order by r_issues.from_entity_id,
             (primary_listing.listing_id is not null) desc,
             r_issues.transaction_time desc,
             dt.ticker asc,
             r_issues.relation_id desc
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
    order by r_listed.from_entity_id,
             r_listed.transaction_time desc,
             dt.ticker asc,
             r_listed.relation_id desc
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
    left join primary_listings primary_listing
      on primary_listing.issuer_id = r_issues.from_entity_id
     and primary_listing.listing_id = r_listed.to_entity_id
    where r_issues.relation_type = 'issues'
      and dl.listing_id is not null
    order by r_issues.from_entity_id,
             (primary_listing.listing_id is not null) desc,
             r_issues.transaction_time desc,
             dl.listing_id asc,
             r_issues.relation_id desc
)
select
    e.entity_id,
    e.kind,
    coalesce(dt.ticker, it.ticker, inst.ticker) as current_ticker,
    coalesce(dn.name, case when upper(btrim(kg.display_name)) <> upper(btrim(coalesce(dt.ticker, it.ticker, inst.ticker, ''))) then nullif(btrim(kg.display_name), '') end) as name,
    dc.cik,
    dl.lei,
    coalesce(dlist.listing_id, ilist.listing_id) as listing_id,
    la.legacy_id
from staging.entities e
join survivors survivor on survivor.entity_id = e.entity_id
left join direct_tickers dt on dt.survivor_id = survivor.id
left join issuer_tickers it on it.issuer_id = survivor.id
left join instrument_tickers inst on inst.instrument_id = survivor.id
left join direct_names dn on dn.survivor_id = survivor.id
left join legacy_aliases la on la.survivor_id = survivor.id
left join direct_ciks dc on dc.survivor_id = survivor.id
left join direct_leis dl on dl.survivor_id = survivor.id
left join direct_listings dlist on dlist.survivor_id = survivor.id
left join issuer_listings ilist on ilist.issuer_id = survivor.id
left join lateral (
    select kg_row.display_name
    from staging.kg_entities kg_row
    where kg_row.id = e.entity_id::text
       or (la.legacy_id is not null and kg_row.id = la.legacy_id)
    order by (kg_row.id = e.entity_id::text) desc,
             nullif(btrim(kg_row.display_name), '') is not null desc,
             kg_row.id
    limit 1
) kg on true
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

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'chk_fund_virtual_company_weight_conservation'
          and conrelid = 'mart.fund_virtual_company'::regclass
    ) then
        delete from mart.fund_virtual_company
        where total_weight_pct < 0
           or total_weight_pct > 105
           or resolved_weight_pct < 0
           or resolved_weight_pct > total_weight_pct
           or valued_weight_pct < 0
           or valued_weight_pct > resolved_weight_pct;

        alter table mart.fund_virtual_company
            add constraint chk_fund_virtual_company_weight_conservation
            check (
                total_weight_pct >= 0
                and total_weight_pct <= 105
                and resolved_weight_pct >= 0
                and resolved_weight_pct <= total_weight_pct
                and valued_weight_pct >= 0
                and valued_weight_pct <= resolved_weight_pct
            );
    end if;
end
$$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'mart_readonly')
       and not has_table_privilege('mart_readonly', 'mart.entity_identity', 'SELECT') then
        grant select on mart.entity_identity to mart_readonly;
    end if;
end
$$;
