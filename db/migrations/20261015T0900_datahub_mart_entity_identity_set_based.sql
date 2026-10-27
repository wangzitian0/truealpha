-- #1337: mart.entity_identity finds survivors and alias ends in one set-based pass.
--
-- Problem: the view of 20260923T1800 called two SQL helpers for each row.
-- It called staging.entity_survivor() 2 times for each alias row and up to 7 times for each
-- identity row. It called staging.entity_alias_valid_to() once or twice for each alias row.
-- On production, 338 identities and 764 aliases gave about 3,900 calls.
-- One evaluation took 1.4 s to 3 s. Each research page evaluates the view several times.
--
-- Rule: this view returns the same rows as the view of 20260923T1800.
-- The columns, their names and their order do not change.
-- The view calls neither helper. These common table expressions give the same values for all rows:
--   survivors          staging.entity_survivor(entity, 'infinity') for every entity, through
--                      merge_edges and survivor_walk. It is the walk of staging.entity_backfill()
--                      in 20260917T0604.
--   alias_retractions  the earliest retraction end of each alias. least() of it and the
--                      alias's own valid_to is staging.entity_alias_valid_to(alias, 'infinity').
-- The alias order stays `confidence desc, transaction_time desc`. When two aliases of one
-- survivor and scheme are equal on both keys, neither definition sets a winner (#1343).
-- Both helpers compare a transaction_time with p_known_at. At 'infinity' each comparison is true,
-- because transaction_time is not null. So the set-based form has no time filter.
-- A change to either helper needs the same change here.
-- libs/runtime/tests/test_entity_identity_view.py compares this view with the old definition.
--
-- Superseded: 20261027T1100_datahub_primary_listing_relation.sql redefines the view later in the
-- chain (#1518) and owns its shape. Replacing the view here would take ACCESS EXCLUSIVE on it on
-- every replay, only for that file to replace it again. So this file creates the view only on a
-- database that has none. 20260923T1800 creates it first, so this file keeps the comment.
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
    order by r_issues.from_entity_id,
             case
                 when dt.ticker = 'GOOGL' then 0
                 when dt.ticker = 'GOOG' then 2
                 else 1
             end,
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
    where r_issues.relation_type = 'issues'
      and dl.listing_id is not null
    order by r_issues.from_entity_id,
             case
                 when dl.listing_id = 'listing:xnas:googl' then 0
                 when dl.listing_id = 'listing:xnas:goog' then 2
                 else 1
             end,
             r_issues.transaction_time desc,
             dl.listing_id asc,
             r_issues.relation_id desc
)
select
    e.entity_id,
    e.kind,
    coalesce(dt.ticker, it.ticker, inst.ticker) as current_ticker,
    coalesce(dn.name, kg.display_name) as name,
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
left join staging.kg_entities kg on (kg.id = e.entity_id::text or (la.legacy_id is not null and kg.id = la.legacy_id))
$view$;
begin
    if to_regclass('mart.entity_identity') is null then
        execute 'create or replace view mart.entity_identity as ' || wanted;
    end if;
end
$$;

comment on view mart.entity_identity is
    '#953, #1337: canonical entity identity projection for mart consumers (entity_id, kind, current_ticker, name, cik, lei, listing_id, legacy_id). One set-based pass; no helper call for each row.';

-- `mart_readonly` and `app_ops_reader` keep select on the view. `create or replace view` keeps
-- the owner and the privileges. db/roles.sql grants them again after the migrations.
