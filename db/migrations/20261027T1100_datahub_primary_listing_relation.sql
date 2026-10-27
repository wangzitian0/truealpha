-- #1518 (Cause 2, step 2): one primary listing for every engine and both views.
--
-- Problem: the view `mart.entity_identity` and the view `mart.entity_display_resolution` ranked the
-- listings of an issuer with the ticker literals 'GOOGL' (first) and 'GOOG' (last). The Python engines
-- held the same rule in four more copies. Production holds entity ids that the entity backfill minted,
-- so the id sets of the engines matched nothing, and the core picked a listing by id text.
--
-- Rule: the relation `primary_listing` (issuer to listing) in `staging.entity_relations` is the one
-- owner. The coordinate resolver writes it from the packaged classification file
-- `primary_listings.v1.json`. This file registers the relation type. Both views read the relation.
-- A listing that a row names comes first. Among the other listings, the order that each view had
-- before the literals stays: the newest relation, then the ticker or the listing id.
-- `data_engine.datahub.primary_listing.choose_primary_listings` is the one reader of the engines.
--
-- This is the last migration that defines both views, so it owns their shape. The earlier files
-- create each view only on a database that has none.
--
-- Boot-lock guard. The chain replays on every boot.
--   * The relation type is inserted only when it is absent. A replay writes no row.
--   * `create or replace view` takes ACCESS EXCLUSIVE on the view. Each view is replaced only when
--     its stored definition differs from the candidate below. A temporary view with the same text
--     gives the comparison through pg_get_viewdef.

insert into staging.entity_relation_types
    (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description, uuid_namespace)
select seed.*, staging.entity_uuid_namespace('relation:' || seed.relation_type)
from (values
    ('primary_listing', 'issuer', 'listing', false, false, 'primary_listing_of',
     'The listing that the engines use for an issuer with several listings. The classification file names it.')
) as seed (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description)
where not exists (select 1 from staging.entity_relation_types as present where present.relation_type = seed.relation_type)
on conflict (relation_type) do nothing;

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
declare
    wanted constant text := $view$
with primary_listings as (
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
entity_tickers as (
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
    left join primary_listings primary_listing
      on primary_listing.issuer_id = (
          case when m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               then m.issuer_id::uuid else null end
      )
     and primary_listing.listing_id = (
          case when m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               then m.listing_id::uuid else null end
      )
    order by m.issuer_id,
             (primary_listing.listing_id is not null) desc,
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
