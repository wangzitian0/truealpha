-- #953: expose the symbolic legacy_id on mart.entity_identity.
--
-- The view already computes it (`legacy_aliases` -> `la.legacy_id`, the `legacy-id`
-- alias of the survivor entity) and used it only to join staging.kg_entities. Projecting
-- it is what lets a mart consumer translate an entity UUID back to the pre-#877 symbolic
-- id it was minted from -- `issuer:lei:...` for an issuer, the same way #954 projected
-- `listing_id` for a listing. Third instance of the same leak (listing_id #954,
-- issuer_id #953), so it is resolved through the SAME view rather than a second
-- translator: one projection, every consumer.

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
    coalesce(dlist.listing_id, ilist.listing_id) as listing_id,
    la.legacy_id
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
    '#953: canonical entity identity projection for mart consumers (entity_id, kind, current_ticker, name, cik, lei, listing_id, legacy_id).';

-- #953: make mart.entity_identity actually READABLE by the role that has to read it.
--
-- A view runs its table references with the VIEW OWNER's privileges, which is why
-- docs/entity-identity.md §6 says "mart_readonly reads it through view-owner permissions,
-- so consumers never touch staging". A FUNCTION called inside the view body does not: a
-- SECURITY INVOKER function executes as the caller. mart.entity_identity calls
-- staging.entity_survivor and staging.entity_alias_valid_to, both of which read staging
-- tables, so every select on the view by mart_readonly (or app_ops_reader) died with
--
--     ERROR: permission denied for schema staging
--       QUERY: with recursive chain(entity_id, depth) as (... staging.entity_relations ...)
--
-- Measured 2026-09-23 on a database built by this chain, against #954/#967's own topt_gppe
-- query: the App twin of that fix has never been able to run. It shipped green because the
-- TypeScript test for it injects a fake query runner and never opens a connection, and the
-- Python twin connects as the superuser, where the invoker check passes.
--
-- The boundary is not widened to fix it: mart_readonly still has no USAGE on staging and
-- no grant on a single staging table. The two helpers become SECURITY DEFINER instead, so
-- the view's own plumbing runs as the view owner exactly like its table references already
-- do. Both are STABLE, fully schema-qualified, take scalar arguments, build no dynamic SQL
-- and return one uuid / one date -- and the search_path is pinned so a caller cannot
-- redirect the qualified names they resolve.
do $$
declare
    fn text;
begin
    foreach fn in array array[
        'staging.entity_survivor(uuid, timestamptz)',
        'staging.entity_alias_valid_to(bigint, timestamptz)'
    ] loop
        -- Guarded on the catalog so a replay that has nothing to change takes no lock.
        if to_regprocedure(fn) is not null
           and not (select p.prosecdef from pg_proc p where p.oid = to_regprocedure(fn))
        then
            execute format('alter function %s security definer', fn);
            execute format('alter function %s set search_path = pg_catalog', fn);
        end if;
    end loop;
end
$$;
