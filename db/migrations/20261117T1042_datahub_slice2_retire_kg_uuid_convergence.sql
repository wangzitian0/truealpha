-- #1730 Slice 2: retire legacy staging.kg_* tables and converge FK identity on staging.entities(entity_id uuid).

insert into staging.entity_alias_schemes
    (scheme, applies_to_kind, is_unique, value_reusable, value_pattern, authority, description)
select seed.*
from (values
    ('ticker', 'entity', false, true, '^[A-Z0-9][A-Z0-9.\-]*$', 'listing symbol without venue MIC',
     'Unqualified trading symbol asserted when venue MIC is unknown.')
) as seed (scheme, applies_to_kind, is_unique, value_reusable, value_pattern, authority, description)
where not exists (select 1 from staging.entity_alias_schemes as present where present.scheme = seed.scheme)
on conflict (scheme) do nothing;

do $$
declare
    v_has_legacy_tables boolean :=
        to_regclass('staging.kg_entities') is not null
        or to_regclass('staging.kg_identifiers') is not null
        or to_regclass('staging.kg_edges') is not null;
    v_needs_fk_uuid boolean :=
        exists (
            select 1
            from information_schema.columns
            where table_schema = 'staging'
              and (
                  (table_name = 'analyst_rating_events' and column_name in ('analyst_id', 'company_id') and data_type <> 'uuid')
                  or (table_name = 'fund_holding_facts' and column_name in ('fund_id', 'holding_id') and data_type <> 'uuid')
              )
        );
    v_now timestamptz := clock_timestamp();
    v_rec record;
    v_uuid uuid;
    v_rel_id uuid;
    v_from_kind text;
    v_to_kind text;
    v_domain_kind text;
    v_range_kind text;
    v_same_kind boolean;
begin
    if not v_has_legacy_tables and not v_needs_fk_uuid then
        return;
    end if;

    create temporary table pg_temp.slice2_id_map (
        raw_id text primary key,
        kind text not null,
        birth_scheme text not null,
        birth_value text not null,
        parsed_scheme text,
        parsed_value text,
        display_name text,
        entity_uuid uuid
    ) on commit drop;

    if to_regclass('staging.kg_entities') is not null then
        insert into pg_temp.slice2_id_map (raw_id, kind, birth_scheme, birth_value, parsed_scheme, parsed_value, display_name)
        select k.id,
               case
                   when k.entity_type in ('etf', 'fund') or k.id ~ '^etf:' then 'fund'
                   when k.entity_type in ('analyst', 'person') or k.id ~ '^analyst:' then 'person'
                   when k.id ~ '^(company:isin:|security:)' then 'instrument'
                   else 'issuer'
               end,
               case
                   when k.id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$' then 'legacy-id'
                   else 'name'
               end,
               case
                   when k.id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$' then k.id
                   else btrim(k.id)
               end,
               case
                   when k.id ~ '^etf:series:S[0-9]{9}$' then 'sec-series'
                   when k.id ~ '^(issuer|company):cik:[0-9]{1,10}$' then 'cik'
                   when k.id ~ '^(issuer|company):lei:[0-9A-Za-z]{18}[0-9]{2}$' then 'lei'
                   when k.id ~ '^(company|security):isin:[A-Za-z]{2}[0-9A-Za-z]{9}[0-9]$' then 'isin'
               end,
               case
                   when k.id ~ '^etf:series:S[0-9]{9}$' then upper(split_part(k.id, ':', 3))
                   when k.id ~ '^(issuer|company):cik:[0-9]{1,10}$' then lpad(split_part(k.id, ':', 3), 10, '0')
                   when k.id ~ '^(issuer|company):lei:[0-9A-Za-z]{18}[0-9]{2}$' then upper(split_part(k.id, ':', 3))
                   when k.id ~ '^(company|security):isin:[A-Za-z]{2}[0-9A-Za-z]{9}[0-9]$' then upper(split_part(k.id, ':', 3))
               end,
               nullif(btrim(k.display_name), '')
        from staging.kg_entities k
        on conflict (raw_id) do nothing;
    end if;

    insert into pg_temp.slice2_id_map (raw_id, kind, birth_scheme, birth_value, parsed_scheme, parsed_value, display_name)
    select distinct s.raw_id, s.kind,
           case
               when s.raw_id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$' then 'legacy-id'
               else 'name'
           end,
           case
               when s.raw_id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$' then s.raw_id
               else btrim(s.raw_id)
           end,
           case
               when s.raw_id ~ '^etf:series:S[0-9]{9}$' then 'sec-series'
               when s.raw_id ~ '^(issuer|company):cik:[0-9]{1,10}$' then 'cik'
               when s.raw_id ~ '^(issuer|company):lei:[0-9A-Za-z]{18}[0-9]{2}$' then 'lei'
               when s.raw_id ~ '^(company|security):isin:[A-Za-z]{2}[0-9A-Za-z]{9}[0-9]$' then 'isin'
           end,
           case
               when s.raw_id ~ '^etf:series:S[0-9]{9}$' then upper(split_part(s.raw_id, ':', 3))
               when s.raw_id ~ '^(issuer|company):cik:[0-9]{1,10}$' then lpad(split_part(s.raw_id, ':', 3), 10, '0')
               when s.raw_id ~ '^(issuer|company):lei:[0-9A-Za-z]{18}[0-9]{2}$' then upper(split_part(s.raw_id, ':', 3))
               when s.raw_id ~ '^(company|security):isin:[A-Za-z]{2}[0-9A-Za-z]{9}[0-9]$' then upper(split_part(s.raw_id, ':', 3))
           end,
           null::text
    from (
        select analyst_id::text as raw_id, 'person'::text as kind from staging.analyst_rating_events
        union
        select company_id::text, case when company_id::text ~ '^etf:' then 'fund' else 'issuer' end from staging.analyst_rating_events
        union
        select fund_id::text, 'fund'::text from staging.fund_holding_facts
        union
        select holding_id::text, case when holding_id::text ~ '^(company:isin:|security:)' then 'instrument' else 'issuer' end
        from staging.fund_holding_facts
        where holding_id is not null
    ) s
    on conflict (raw_id) do nothing;

    -- Map existing UUIDs directly to their canonical survivor in staging.entities.
    update pg_temp.slice2_id_map m
       set entity_uuid = staging.entity_survivor(e.entity_id, 'infinity'::timestamptz)
      from staging.entities e
     where m.raw_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       and e.entity_id = m.raw_id::uuid;

    -- Resolve remaining entries via active aliases or birth coordinates before minting.
    for v_rec in
        select raw_id, kind, birth_scheme, birth_value, parsed_scheme, parsed_value, display_name
        from pg_temp.slice2_id_map
        where entity_uuid is null
        order by (parsed_scheme in ('cik', 'lei', 'sec-series', 'isin')) desc nulls last, raw_id
    loop
        v_uuid := null;
        if v_rec.raw_id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$' then
            select staging.entity_survivor(ea.entity_id, 'infinity'::timestamptz) into v_uuid
            from staging.entity_aliases ea
            where ea.scheme = 'legacy-id' and ea.value = v_rec.raw_id
            order by ea.transaction_time desc, ea.alias_id desc
            limit 1;
        end if;
        if v_uuid is null and v_rec.parsed_scheme is not null then
            select staging.entity_survivor(ea.entity_id, 'infinity'::timestamptz) into v_uuid
            from staging.entity_aliases ea
            where ea.scheme = v_rec.parsed_scheme
              and ea.value = staging.entity_alias_normalize(v_rec.parsed_scheme, v_rec.parsed_value)
            order by ea.transaction_time desc, ea.alias_id desc
            limit 1;
        end if;
        if v_uuid is null then
            select staging.entity_survivor(e.entity_id, 'infinity'::timestamptz) into v_uuid
            from staging.entities e
            where e.kind = v_rec.kind
              and e.birth_scheme = v_rec.birth_scheme
              and e.birth_value = staging.entity_alias_normalize(v_rec.birth_scheme, v_rec.birth_value)
              and e.birth_generation = 1
            limit 1;
        end if;
        if v_uuid is null and v_rec.birth_scheme = 'name' then
            select staging.entity_survivor(e.entity_id, 'infinity'::timestamptz) into v_uuid
            from staging.entities e
            where e.kind = v_rec.kind
              and e.birth_scheme = 'name'
              and e.birth_value = btrim(v_rec.birth_value)
              and e.birth_generation = 1
            limit 1;
        end if;
        if v_uuid is null then
            v_uuid := staging.entity_mint(v_rec.kind, v_rec.birth_scheme, v_rec.birth_value, 'slice2:migration');
        end if;

        update pg_temp.slice2_id_map set entity_uuid = v_uuid where raw_id = v_rec.raw_id;

        if v_rec.birth_scheme = 'name'
           and not exists (
               select 1 from staging.entity_aliases ea
               where ea.entity_id = v_uuid and ea.scheme = 'name' and ea.value = btrim(v_rec.birth_value)
           ) then
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (v_uuid, 'name', btrim(v_rec.birth_value), '-infinity'::date, v_now, 'slice2:migration', 'migration:slice2', 'asserted', 1.0, 'slice2:v1')
            on conflict do nothing;
        end if;

        if v_rec.parsed_scheme is not null
           and not exists (
               select 1 from staging.entity_aliases ea
               where ea.entity_id = v_uuid
                 and ea.scheme = v_rec.parsed_scheme
                 and ea.value = staging.entity_alias_normalize(v_rec.parsed_scheme, v_rec.parsed_value)
           ) then
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            select v_uuid, v_rec.parsed_scheme, staging.entity_alias_normalize(v_rec.parsed_scheme, v_rec.parsed_value),
                   '-infinity'::date, v_now, 'slice2:migration', 'migration:slice2', 'asserted', 1.0, 'slice2:v1'
            from staging.entities e
            join staging.entity_alias_schemes s on s.scheme = v_rec.parsed_scheme
            where e.entity_id = v_uuid
              and staging.entity_kind_is_a(e.kind, s.applies_to_kind)
              and staging.entity_alias_normalize(v_rec.parsed_scheme, v_rec.parsed_value) ~ s.value_pattern
              and (
                  not s.is_unique
                  or not exists (
                      select 1 from staging.entity_aliases ex
                      where ex.scheme = v_rec.parsed_scheme
                        and ex.value = staging.entity_alias_normalize(v_rec.parsed_scheme, v_rec.parsed_value)
                        and ex.entity_id <> v_uuid
                  )
              )
            on conflict do nothing;
        end if;

        if v_rec.raw_id ~ '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$'
           and not exists (
               select 1 from staging.entity_aliases ea where ea.scheme = 'legacy-id' and ea.value = v_rec.raw_id
           ) then
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (v_uuid, 'legacy-id', v_rec.raw_id, '-infinity'::date, v_now, 'slice2:migration', 'migration:slice2', 'asserted', 1.0, 'slice2:v1')
            on conflict do nothing;
        end if;

        if v_rec.display_name is not null
           and v_rec.display_name <> v_rec.raw_id
           and v_rec.display_name ~ '^\S(.*\S)?$'
           and not exists (
               select 1 from staging.entity_aliases ea
               where ea.entity_id = v_uuid and ea.scheme = 'name' and ea.value = v_rec.display_name
           ) then
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (v_uuid, 'name', v_rec.display_name, '-infinity'::date, v_now, 'slice2:migration', 'migration:slice2', 'asserted', 0.9, 'slice2:v1')
            on conflict do nothing;
        end if;
    end loop;

    -- Preserve valid identifiers from staging.kg_identifiers into staging.entity_aliases.
    if to_regclass('staging.kg_identifiers') is not null then
        for v_rec in
            select m.entity_uuid,
                   case ki.identifier_type
                       when 'sec_series' then 'sec-series'
                       when 'moomoo' then 'moomoo-code'
                       else ki.identifier_type
                   end as scheme,
                   staging.entity_alias_normalize(
                       case ki.identifier_type when 'sec_series' then 'sec-series' when 'moomoo' then 'moomoo-code' else ki.identifier_type end,
                       ki.identifier_value
                   ) as norm_value,
                   coalesce(lower(ki.valid_time), '-infinity'::date) as valid_from,
                   upper(ki.valid_time) as valid_to,
                   least(ki.transaction_time, v_now) as tx_time,
                   coalesce(nullif(btrim(ki.source), ''), 'slice2:migration') as source,
                   coalesce(nullif(btrim(ki.raw_ref), ''), 'migration:slice2') as raw_ref,
                   ki.confidence
            from staging.kg_identifiers ki
            join pg_temp.slice2_id_map m on m.raw_id = ki.entity_id
            where m.entity_uuid is not null
            order by ki.transaction_time, ki.id
        loop
            continue when v_rec.norm_value is null;
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref, method, confidence, mapping_version)
            select v_rec.entity_uuid, v_rec.scheme, v_rec.norm_value, v_rec.valid_from, v_rec.valid_to,
                   v_rec.tx_time, v_rec.source, v_rec.raw_ref, 'asserted', v_rec.confidence, 'slice2:v1'
            from staging.entities e
            join staging.entity_alias_schemes s on s.scheme = v_rec.scheme
            where e.entity_id = v_rec.entity_uuid
              and staging.entity_kind_is_a(e.kind, s.applies_to_kind)
              and v_rec.norm_value ~ s.value_pattern
              and (
                  not s.is_unique
                  or not exists (
                      select 1 from staging.entity_aliases ex
                      where ex.scheme = v_rec.scheme and ex.value = v_rec.norm_value and ex.entity_id <> v_rec.entity_uuid
                  )
              )
            on conflict do nothing;
        end loop;
    end if;

    -- Preserve valid edges from staging.kg_edges into staging.entity_relations.
    if to_regclass('staging.kg_edges') is not null then
        for v_rec in
            select mf.entity_uuid as from_uuid,
                   mt.entity_uuid as to_uuid,
                   e.relation_type,
                   coalesce(lower(e.valid_time), '-infinity'::date) as valid_from,
                   upper(e.valid_time) as valid_to,
                   least(e.transaction_time, v_now) as tx_time,
                   coalesce(nullif(btrim(e.source), ''), 'slice2:migration') as source,
                   coalesce(nullif(btrim(e.raw_ref), ''), 'migration:slice2') as raw_ref,
                   e.confidence
            from staging.kg_edges e
            join pg_temp.slice2_id_map mf on mf.raw_id = e.from_id
            join pg_temp.slice2_id_map mt on mt.raw_id = e.to_id
            where mf.entity_uuid is not null and mt.entity_uuid is not null and mf.entity_uuid <> mt.entity_uuid
            order by e.transaction_time, e.id
        loop
            select kind into v_from_kind from staging.entities where entity_id = v_rec.from_uuid;
            select kind into v_to_kind from staging.entities where entity_id = v_rec.to_uuid;

            -- Convert cross-kind same_as(instrument -> issuer) into issues(issuer -> instrument).
            if v_rec.relation_type = 'same_as' and v_from_kind = 'instrument' and staging.entity_kind_is_a(v_to_kind, 'issuer') then
                v_rel_id := staging.entity_relation_uuid('issues', v_rec.to_uuid, v_rec.from_uuid, v_rec.valid_from, v_rec.tx_time, v_rec.source, 'asserted');
                insert into staging.entity_relations
                    (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, valid_to, transaction_time, source, raw_ref, method, confidence, mapping_version)
                values (v_rel_id, 'issues', v_rec.to_uuid, v_rec.from_uuid, v_rec.valid_from, v_rec.valid_to, v_rec.tx_time, v_rec.source, v_rec.raw_ref, 'asserted', v_rec.confidence, 'slice2:v1')
                on conflict do nothing;
                continue;
            end if;

            select domain_kind, range_kind, same_kind into v_domain_kind, v_range_kind, v_same_kind
            from staging.entity_relation_types where relation_type = v_rec.relation_type;
            if v_domain_kind is null then
                continue;
            end if;
            if staging.entity_kind_is_a(v_from_kind, v_domain_kind)
               and staging.entity_kind_is_a(v_to_kind, v_range_kind)
               and (not v_same_kind or v_from_kind = v_to_kind) then
                v_rel_id := staging.entity_relation_uuid(v_rec.relation_type, v_rec.from_uuid, v_rec.to_uuid, v_rec.valid_from, v_rec.tx_time, v_rec.source, 'asserted');
                insert into staging.entity_relations
                    (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, valid_to, transaction_time, source, raw_ref, method, confidence, mapping_version)
                values (v_rel_id, v_rec.relation_type, v_rec.from_uuid, v_rec.to_uuid, v_rec.valid_from, v_rec.valid_to, v_rec.tx_time, v_rec.source, v_rec.raw_ref, 'asserted', v_rec.confidence, 'slice2:v1')
                on conflict do nothing;
            end if;
        end loop;
    end if;

    -- Drop dependent views before column-type alters and legacy table drops.
    drop view if exists mart.fund_holdings_valuation;
    drop view if exists mart.fund_holdings_coverage;
    drop view if exists mart.fund_holdings_resolved;
    drop view if exists mart.fund_holdings;
    drop view if exists mart.entity_identity;
    drop view if exists mart.entity_display_resolution;
    drop view if exists staging.entity_backfill_plan;

    create or replace function pg_temp.slice2_lookup_uuid(p_raw text)
    returns uuid language sql stable as $fn$
        select entity_uuid from pg_temp.slice2_id_map where raw_id = p_raw;
    $fn$;

    -- Convert staging.analyst_rating_events FKs and columns to uuid -> staging.entities(entity_id).
    alter table staging.analyst_rating_events drop constraint if exists analyst_rating_events_analyst_id_fkey;
    alter table staging.analyst_rating_events drop constraint if exists analyst_rating_events_company_id_fkey;

    if exists (
        select 1 from information_schema.columns
        where table_schema = 'staging' and table_name = 'analyst_rating_events'
          and column_name in ('analyst_id', 'company_id') and data_type <> 'uuid'
    ) then
        alter table staging.analyst_rating_events disable trigger trg_analyst_rating_events_append_only;
        delete from staging.analyst_rating_events a
        using (
            select id,
                   row_number() over (
                       partition by ma.entity_uuid, mc.entity_uuid, e.recommendation_at, e.transaction_time
                       order by (e.company_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') desc, e.id desc
                   ) as rn
            from staging.analyst_rating_events e
            join pg_temp.slice2_id_map ma on ma.raw_id = e.analyst_id::text
            join pg_temp.slice2_id_map mc on mc.raw_id = e.company_id::text
        ) ranked
        where a.id = ranked.id and ranked.rn > 1;
        alter table staging.analyst_rating_events enable trigger trg_analyst_rating_events_append_only;

        alter table staging.analyst_rating_events
            alter column analyst_id type uuid using pg_temp.slice2_lookup_uuid(analyst_id::text),
            alter column company_id type uuid using pg_temp.slice2_lookup_uuid(company_id::text);
    end if;

    alter table staging.analyst_rating_events
        add constraint analyst_rating_events_analyst_id_fkey FOREIGN KEY (analyst_id) REFERENCES staging.entities(entity_id),
        add constraint analyst_rating_events_company_id_fkey FOREIGN KEY (company_id) REFERENCES staging.entities(entity_id);

    -- Convert staging.fund_holding_facts FKs and columns to uuid -> staging.entities(entity_id).
    alter table staging.fund_holding_facts drop constraint if exists fund_holding_facts_fund_id_fkey;
    alter table staging.fund_holding_facts drop constraint if exists fund_holding_facts_holding_id_fkey;

    if exists (
        select 1 from information_schema.columns
        where table_schema = 'staging' and table_name = 'fund_holding_facts'
          and column_name in ('fund_id', 'holding_id') and data_type <> 'uuid'
    ) then
        alter table staging.fund_holding_facts disable trigger trg_fund_holding_facts_append_only;
        delete from staging.fund_holding_facts f
        using (
            select id,
                   row_number() over (
                       partition by mf.entity_uuid, e.holding_name, e.report_period, e.transaction_time, coalesce(e.isin, ''), coalesce(e.cusip, '')
                       order by e.id desc
                   ) as rn
            from staging.fund_holding_facts e
            join pg_temp.slice2_id_map mf on mf.raw_id = e.fund_id::text
        ) ranked
        where f.id = ranked.id and ranked.rn > 1;
        alter table staging.fund_holding_facts enable trigger trg_fund_holding_facts_append_only;

        alter table staging.fund_holding_facts
            alter column fund_id type uuid using pg_temp.slice2_lookup_uuid(fund_id::text),
            alter column holding_id type uuid using (
                case when holding_id is null then null else pg_temp.slice2_lookup_uuid(holding_id::text) end
            );
    end if;

    drop function if exists pg_temp.slice2_lookup_uuid(text);

    alter table staging.fund_holding_facts
        add constraint fund_holding_facts_fund_id_fkey FOREIGN KEY (fund_id) REFERENCES staging.entities(entity_id),
        add constraint fund_holding_facts_holding_id_fkey FOREIGN KEY (holding_id) REFERENCES staging.entities(entity_id);

    -- Unblock retiring staging.kg_entities when superseded staging.market_prices exists on upgraded DBs.
    if to_regclass('staging.market_prices') is not null then
        alter table staging.market_prices
            drop constraint if exists market_prices_unified_id_fkey;
        if not exists (select 1 from staging.market_prices limit 1) then
            drop table staging.market_prices;
        end if;
    end if;

    -- Retire legacy kg_* tables and indexes.
    drop index if exists staging.idx_kg_edges_to;
    drop index if exists staging.idx_kg_edges_asof;
    drop index if exists staging.uq_kg_edges_vintage;
    drop index if exists staging.idx_kg_identifiers_entity_type;
    drop index if exists staging.idx_kg_identifiers_type_value;
    drop index if exists staging.idx_kg_identifiers_asof;
    drop table if exists staging.kg_edges;
    drop table if exists staging.kg_identifiers;
    drop table if exists staging.kg_entities;
end
$$;

-- Reconcile functions and views on upgraded databases (no-op on fresh databases where 0002/0003 created them).
do $$
begin
    if pg_get_functiondef('staging.entity_backfill_due(timestamptz)'::regprocedure) ~* '\bstaging\.(kg_entities|kg_identifiers|kg_edges)\b' then
        execute $fn$
        CREATE OR REPLACE FUNCTION staging.entity_backfill_due(p_since timestamp with time zone)
         RETURNS text
         LANGUAGE sql
         STABLE
         SET jit TO 'off'
        AS $function$
            with used(legacy_id) as (
                select distinct used.legacy_id
                from staging.capture_observation_payloads payload
                cross join lateral (values
                    (case when jsonb_typeof(payload.normalized_payload->'issuer_id') = 'string'
                          then payload.normalized_payload->>'issuer_id' end),
                    (case when jsonb_typeof(payload.normalized_payload->'instrument_id') = 'string'
                          then payload.normalized_payload->>'instrument_id' end),
                    (case when jsonb_typeof(payload.normalized_payload->'listing_id') = 'string'
                          then payload.normalized_payload->>'listing_id' end)) as used(legacy_id)
                where used.legacy_id is not null
                union
                select member->>position
                from staging.contract_objects head
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(head.payload->'instruments') = 'array'
                         then head.payload->'instruments' else '[]'::jsonb end) member
                cross join (values (0), (1), (2)) as slot(position)
                where head.contract_kind like 'universe-list:%'
                  and jsonb_typeof(member) = 'array'
                  and jsonb_typeof(member->position) = 'string'
                  and member->>position is not null
            )
            select case
                when not exists (select 1 from staging.entities) and exists (select 1 from used)
                    then 'store-empty'
                when exists (
                    select 1 from used
                    where not exists (
                        select 1 from staging.entity_aliases alias
                        where alias.scheme = 'legacy-id' and alias.value = used.legacy_id))
                    then 'unseen-legacy-id'
                when exists (
                        select 1 from staging.fund_holding_facts
                        where recorded_at > coalesce(p_since, '-infinity'::timestamptz))
                  or exists (
                        select 1 from staging.entity_aliases
                        where scheme = 'isin'
                          and mapping_version not like 'entity-backfill%'
                          and recorded_at > coalesce(p_since, '-infinity'::timestamptz))
                  or exists (
                        select 1 from staging.entity_relations
                        where mapping_version not like 'entity-backfill%'
                          and recorded_at > coalesce(p_since, '-infinity'::timestamptz))
                    then 'new-crosswalk-evidence'
            end;
        $function$;
        $fn$;
    end if;

    if pg_get_functiondef('staging.entity_evidence_watermark()'::regprocedure) ~* '\bstaging\.(kg_entities|kg_identifiers|kg_edges)\b' then
        execute $fn$
        CREATE OR REPLACE FUNCTION staging.entity_evidence_watermark()
         RETURNS timestamp with time zone
         LANGUAGE sql
         STABLE
         SET jit TO 'off'
        AS $function$
            select greatest(
                (select max(recorded_at) from staging.capture_normalized_observations),
                (select max(recorded_at) from staging.contract_objects where contract_kind like 'universe-list:%'),
                (select max(recorded_at) from staging.fund_holding_facts),
                (select max(recorded_at) from staging.entity_aliases where scheme = 'isin' and mapping_version not like 'entity-backfill%'),
                (select max(recorded_at) from staging.entity_relations where mapping_version not like 'entity-backfill%'));
        $function$;
        $fn$;
    end if;

    if to_regclass('staging.entity_backfill_plan') is null then
        execute $vc$create or replace view staging.entity_backfill_plan as
        with usage as (
            select payload.normalized_payload->>'issuer_id' as issuer_id,
                   payload.normalized_payload->>'instrument_id' as instrument_id,
                   payload.normalized_payload->>'listing_id' as listing_id,
                   coalesce(
                       min(observation.knowable_at)
                           filter (where observation.semantic_type in ('listing-identity', 'universe-membership')),
                       min(observation.knowable_at)) as known_at,
                   (min(observation.valid_from) at time zone 'UTC')::date as valid_from,
                   coalesce(
                       max(observation.confidence)
                           filter (where observation.semantic_type in ('listing-identity', 'universe-membership')),
                       max(observation.confidence)) as confidence,
                   min(observation.observation_id) as raw_ref,
                   'capture-observation' as source
            from staging.capture_observation_payloads payload
            join staging.capture_normalized_observations observation
              on observation.observation_id = payload.observation_id
            where jsonb_typeof(payload.normalized_payload->'issuer_id') = 'string'
              and jsonb_typeof(payload.normalized_payload->'instrument_id') = 'string'
              and jsonb_typeof(payload.normalized_payload->'listing_id') = 'string'
            group by 1, 2, 3
            union all
            select member->>0,
                   member->>1,
                   member->>2,
                   min(head.recorded_at),
                   min(case when head.payload->>'report_date' ~ '^\d{4}-\d{2}-\d{2}$'
                            then (head.payload->>'report_date')::date end),
                   1.0,
                   min(head.contract_id),
                   'universe-head'
            from staging.contract_objects head
            cross join lateral jsonb_array_elements(
                case when jsonb_typeof(head.payload->'instruments') = 'array'
                     then head.payload->'instruments' else '[]'::jsonb end) member
            where head.contract_kind like 'universe-list:%'
              and head.payload->'instrument_tuple_fields'
                  = '["issuer_id", "instrument_id", "listing_id", "ticker"]'::jsonb
              and jsonb_typeof(member) = 'array'
              and jsonb_typeof(member->0) = 'string'
              and jsonb_typeof(member->1) = 'string'
              and jsonb_typeof(member->2) = 'string'
            group by 1, 2, 3
        ), trio as (
            select issuer_id, instrument_id, listing_id,
                   min(known_at) as known_at,
                   coalesce(min(valid_from), (min(known_at) at time zone 'UTC')::date) as valid_from,
                   max(confidence) as confidence,
                   (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
                   (array_agg(source order by known_at, raw_ref))[1] as source
            from usage
            group by 1, 2, 3
        ), legacy as (
            select role, legacy_id,
                   min(known_at) as known_at,
                   min(valid_from) as valid_from,
                   (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
                   (array_agg(source order by known_at, raw_ref))[1] as source,
                   count(*) over (partition by legacy_id) as roles
            from (
                select 'issuer' as role, issuer_id as legacy_id, known_at, valid_from, raw_ref, source from trio
                union all
                select 'instrument', instrument_id, known_at, valid_from, raw_ref, source from trio
                union all
                select 'listing', listing_id, known_at, valid_from, raw_ref, source from trio
            ) used
            group by role, legacy_id
        ), parsed as (
            select legacy.*,
                   case
                       when role = 'issuer' and legacy_id ~ '^issuer:lei:[0-9A-Za-z]{20}$' then 'lei'
                       when role = 'issuer' and legacy_id ~ '^issuer:cik:[0-9]{1,10}$' then 'cik'
                       when role = 'instrument' and legacy_id ~ '^security:cusip:[0-9A-Za-z*@#]{9}$' then 'cusip'
                       when role = 'instrument' and legacy_id ~ '^security:figi:[0-9A-Za-z]{12}$' then 'figi'
                       when role = 'listing' and legacy_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$' then 'mic-ticker'
                   end as scheme,
                   case
                       when role = 'issuer' and legacy_id ~ '^issuer:lei:[0-9A-Za-z]{20}$'
                           then upper(split_part(legacy_id, ':', 3))
                       when role = 'issuer' and legacy_id ~ '^issuer:cik:[0-9]{1,10}$'
                           then lpad(split_part(legacy_id, ':', 3), 10, '0')
                       when role = 'instrument' and legacy_id ~ '^security:cusip:[0-9A-Za-z*@#]{9}$'
                           then upper(split_part(legacy_id, ':', 3))
                       when role = 'instrument' and legacy_id ~ '^security:figi:[0-9A-Za-z]{12}$'
                           then upper(split_part(legacy_id, ':', 3))
                       when role = 'listing' and legacy_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
                           then upper(split_part(legacy_id, ':', 2)) || ':' || upper(split_part(legacy_id, ':', 3))
                   end as value
            from legacy
        ), proof as (
            select line.id as line_id,
                   upper(line.lei) as lei,
                   upper(line.cusip) as cusip,
                   upper(line.isin) as isin,
                   line.report_period,
                   cik_alias.value as cik,
                   greatest(line.transaction_time, isin_alias.transaction_time, cik_alias.transaction_time, coalesce(rel.transaction_time, line.transaction_time)) as known_at,
                   least(line.confidence, isin_alias.confidence, cik_alias.confidence, coalesce(rel.confidence, 1.0)) as confidence,
                   line.raw_ref,
                   jsonb_build_object(
                       'fund_holding_fact_id', line.id,
                       'nport_raw_ref', line.raw_ref,
                       'isin_alias_id', isin_alias.alias_id,
                       'isin_raw_ref', isin_alias.raw_ref,
                       'issues_relation_id', rel.relation_id,
                       'issuer_entity_id', coalesce(rel.from_entity_id, isin_alias.entity_id)) as evidence
            from staging.fund_holding_facts line
            cross join lateral (
                select ea.alias_id, ea.entity_id, ea.transaction_time, ea.confidence, ea.raw_ref
                from staging.entity_aliases ea
                where ea.scheme = 'isin' and ea.value = upper(line.isin)
                order by ea.transaction_time desc, ea.confidence desc, ea.alias_id desc
                limit 1
            ) isin_alias
            left join lateral (
                select r.relation_id, r.from_entity_id, r.transaction_time, r.confidence
                from staging.entity_relations r
                where r.to_entity_id = isin_alias.entity_id and r.relation_type = 'issues'
                order by r.transaction_time desc, r.relation_id desc
                limit 1
            ) rel on true
            cross join lateral (
                select ea.value, ea.transaction_time, ea.confidence
                from staging.entity_aliases ea
                where ea.entity_id = staging.entity_survivor(coalesce(rel.from_entity_id, isin_alias.entity_id), 'infinity'::timestamptz)
                  and ea.scheme = 'cik'
                order by ea.transaction_time desc, ea.confidence desc, ea.alias_id desc
                limit 1
            ) cik_alias
            where line.lei ~ '^[0-9A-Za-z]{20}$'
              and line.isin ~ '^[0-9A-Za-z]{12}$'
        ), lei_link as (
            select lei, count(distinct cik) as ciks, min(cik) as cik, min(report_period) as valid_from,
                   (array_agg(known_at order by known_at, line_id))[1] as known_at,
                   (array_agg(confidence order by known_at, line_id))[1] as confidence,
                   (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
                   (array_agg(evidence order by known_at, line_id))[1] as evidence
            from proof group by lei
        ), cik_leis as (
            select cik, count(distinct lei) as leis from proof group by cik
        ), cusip_isin as (
            select cusip, count(distinct isin) as isins, count(distinct cik) as ciks,
                   min(isin) as isin, min(cik) as cik, min(report_period) as valid_from,
                   (array_agg(known_at order by known_at, line_id))[1] as known_at,
                   (array_agg(confidence order by known_at, line_id))[1] as confidence,
                   (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
                   (array_agg(evidence order by known_at, line_id))[1] as evidence
            from proof where cusip ~ '^[0-9A-Z*@#]{9}$' group by cusip
        ), isin_cusips as (
            select isin, count(distinct cusip) as cusips from proof where cusip is not null group by isin
        ), issuer_component as (
            select parsed.legacy_id,
                   case
                       when parsed.roles > 1 then 'issuer/legacy=' || parsed.legacy_id
                       when parsed.scheme = 'cik' then 'issuer/cik=' || parsed.value
                       when parsed.scheme = 'lei' and lei_link.ciks = 1 and cik_leis.leis = 1 then 'issuer/cik=' || lei_link.cik
                       when parsed.scheme = 'lei' then 'issuer/lei=' || parsed.value
                       else 'issuer/legacy=' || parsed.legacy_id
                   end as component,
                   case
                       when parsed.roles > 1 then 'conflict:id-used-in-several-roles'
                       when parsed.scheme <> 'lei' or parsed.scheme is null then null
                       when lei_link.lei is null then 'unlinked:no-crosswalk'
                       when lei_link.ciks > 1 then 'unlinked:lei-proves-several-ciks'
                       when cik_leis.leis > 1 then 'unlinked:cik-proven-for-several-leis'
                   end as reason
            from parsed
            left join lei_link on parsed.scheme = 'lei' and lei_link.lei = parsed.value
            left join cik_leis on cik_leis.cik = lei_link.cik
            where parsed.role = 'issuer'
        ), pair as (
            select cusip_trio.instrument_id as cusip_id, figi_trio.instrument_id as figi_id,
                   cusip_trio.listing_id,
                   greatest(cusip_trio.known_at, figi_trio.known_at, cusip_isin.known_at) as known_at,
                   least(cusip_trio.confidence, figi_trio.confidence, cusip_isin.confidence) as confidence,
                   jsonb_build_object(
                       'shared_listing_id', cusip_trio.listing_id,
                       'cusip_trio_raw_ref', cusip_trio.raw_ref,
                       'figi_trio_raw_ref', figi_trio.raw_ref,
                       'issuer_component', cusip_issuer.component,
                       'nport_proof', cusip_isin.evidence) as evidence,
                   cusip_isin.raw_ref
            from trio cusip_trio
            join trio figi_trio on figi_trio.listing_id = cusip_trio.listing_id
            join parsed cusip_parsed on cusip_parsed.role = 'instrument' and cusip_parsed.legacy_id = cusip_trio.instrument_id and cusip_parsed.scheme = 'cusip'
            join parsed figi_parsed on figi_parsed.role = 'instrument' and figi_parsed.legacy_id = figi_trio.instrument_id and figi_parsed.scheme = 'figi'
            join issuer_component cusip_issuer on cusip_issuer.legacy_id = cusip_trio.issuer_id
            join issuer_component figi_issuer on figi_issuer.legacy_id = figi_trio.issuer_id
            join cusip_isin on cusip_isin.cusip = cusip_parsed.value
            where cusip_issuer.component = figi_issuer.component
              and cusip_issuer.component like 'issuer/cik=%'
              and cusip_isin.ciks = 1
              and 'issuer/cik=' || cusip_isin.cik = cusip_issuer.component
        ), cusip_link as (
            select cusip_id, count(distinct figi_id) as figis, min(figi_id) as figi_id,
                   (array_agg(known_at order by known_at, figi_id))[1] as known_at,
                   (array_agg(confidence order by known_at, figi_id))[1] as confidence,
                   (array_agg(raw_ref order by known_at, figi_id))[1] as raw_ref,
                   (array_agg(evidence order by known_at, figi_id))[1] as evidence
            from pair group by cusip_id
        ), figi_cusips as (
            select figi_id, count(distinct cusip_id) as cusips from pair group by figi_id
        ), instrument_component as (
            select parsed.legacy_id,
                   case
                       when parsed.roles > 1 then 'instrument/legacy=' || parsed.legacy_id
                       when parsed.scheme = 'figi' then 'instrument/figi=' || parsed.value
                       when parsed.scheme = 'cusip' and cusip_link.figis = 1 and figi_cusips.cusips = 1 then 'instrument/figi=' || figi_parsed.value
                       when parsed.scheme = 'cusip' then 'instrument/cusip=' || parsed.value
                       else 'instrument/legacy=' || parsed.legacy_id
                   end as component,
                   case
                       when parsed.roles > 1 then 'conflict:id-used-in-several-roles'
                       when parsed.scheme <> 'cusip' or parsed.scheme is null then null
                       when cusip_link.cusip_id is null and not exists (
                           select 1
                           from trio mine
                           join trio other on other.listing_id = mine.listing_id
                           join parsed other_parsed
                             on other_parsed.role = 'instrument' and other_parsed.legacy_id = other.instrument_id
                            and other_parsed.scheme = 'figi'
                           where mine.instrument_id = parsed.legacy_id)
                           then 'unlinked:no-counterpart'
                       when cusip_link.cusip_id is null then 'unlinked:no-crosswalk'
                       when cusip_link.figis > 1 or figi_cusips.cusips > 1 then 'unlinked:ambiguous-counterpart'
                   end as reason
            from parsed
            left join cusip_link on parsed.scheme = 'cusip' and cusip_link.cusip_id = parsed.legacy_id
            left join figi_cusips on figi_cusips.figi_id = cusip_link.figi_id
            left join parsed figi_parsed
              on figi_parsed.role = 'instrument' and figi_parsed.legacy_id = cusip_link.figi_id
            where parsed.role = 'instrument'
        ), listing_component as (
            select parsed.legacy_id,
                   case
                       when parsed.roles > 1 then 'listing/legacy=' || parsed.legacy_id
                       when parsed.scheme = 'mic-ticker' then 'listing/mic-ticker=' || parsed.value
                       else 'listing/legacy=' || parsed.legacy_id
                   end as component,
                   case when parsed.roles > 1 then 'conflict:id-used-in-several-roles' end as reason
            from parsed where parsed.role = 'listing'
        ), component as (
            select 'issuer' as kind, legacy_id, component, reason from issuer_component
            union all
            select 'instrument', legacy_id, component, reason from instrument_component
            union all
            select 'listing', legacy_id, component, reason from listing_component
        ), component_state as (
            select component.*,
                   count(*) over (partition by component.kind, component.component) as legacy_ids,
                   parsed.scheme, parsed.value, parsed.known_at, parsed.valid_from, parsed.raw_ref, parsed.source,
                   case
                       when component.reason like 'conflict:%' then component.reason
                       when count(*) over (partition by component.kind, component.component) > 1 then 'linked'
                       when component.reason is not null then component.reason
                       when parsed.scheme = 'lei' then 'cik-proven:no-counterpart'
                       else 'single-scheme'
                   end as link_state
            from component
            join parsed on parsed.role = component.kind and parsed.legacy_id = component.legacy_id
        ), alias_claim as (
            select kind, component, 'legacy-id' as scheme, legacy_id as value,
                   valid_from, known_at, source, raw_ref, 'asserted' as method, 1.0 as confidence,
                   jsonb_build_object('used_by', source) as evidence, legacy_id, link_state
            from component_state
            union all
            select kind, component, scheme, value,
                   valid_from, known_at, source, raw_ref, 'parsed', 1.0,
                   jsonb_build_object('parsed_from', legacy_id), legacy_id, link_state
            from component_state
            where scheme is not null
            union all
            select state.kind, state.component, 'cik', lei_link.cik,
                   lei_link.valid_from, lei_link.known_at, 'nport+kg', lei_link.raw_ref, 'crosswalk', lei_link.confidence,
                   lei_link.evidence || jsonb_build_object('lei', state.value), state.legacy_id, state.link_state
            from component_state state
            join lei_link on lei_link.lei = state.value
            where state.kind = 'issuer' and state.scheme = 'lei' and state.component = 'issuer/cik=' || lei_link.cik
            union all
            select state.kind, state.component, 'isin', cusip_isin.isin,
                   cusip_isin.valid_from, cusip_isin.known_at, 'nport+kg', cusip_isin.raw_ref, 'crosswalk', cusip_isin.confidence,
                   cusip_isin.evidence || jsonb_build_object('cusip', state.value), state.legacy_id, state.link_state
            from component_state state
            join cusip_isin on cusip_isin.cusip = state.value
            join isin_cusips on isin_cusips.isin = cusip_isin.isin
            join trio on trio.instrument_id = state.legacy_id
            join issuer_component issuer on issuer.legacy_id = trio.issuer_id
            where state.kind = 'instrument' and state.scheme = 'cusip'
              and cusip_isin.isins = 1 and cusip_isin.ciks = 1 and isin_cusips.cusips = 1
              and (substring(cusip_isin.isin from 1 for 2) not in ('US', 'CA')
                   or substring(cusip_isin.isin from 3 for 9) = cusip_isin.cusip)
              and issuer.component = 'issuer/cik=' || cusip_isin.cik
            group by state.kind, state.component, cusip_isin.isin, cusip_isin.valid_from, cusip_isin.known_at,
                     cusip_isin.raw_ref, cusip_isin.confidence, cusip_isin.evidence, state.value, state.legacy_id, state.link_state
            union all
            select state.kind, state.component, 'cusip', state.value,
                   state.valid_from, cusip_link.known_at, 'shared-listing+nport+kg', cusip_link.raw_ref, 'crosswalk', cusip_link.confidence,
                   cusip_link.evidence || jsonb_build_object('figi_legacy_id', cusip_link.figi_id), state.legacy_id, state.link_state
            from component_state state
            join cusip_link on cusip_link.cusip_id = state.legacy_id
            where state.kind = 'instrument' and state.scheme = 'cusip' and state.component like 'instrument/figi=%'
        ), relation_use as (
            select 'issues' as relation_type, 'issuer' as from_kind, issuer.component as from_component,
                   'instrument' as to_kind, instrument.component as to_component, trio.known_at, trio.valid_from,
                   trio.confidence, trio.raw_ref, trio.source,
                   issuer.reason like 'conflict:%' or instrument.reason like 'conflict:%' as blocked
            from trio
            join issuer_component issuer on issuer.legacy_id = trio.issuer_id
            join instrument_component instrument on instrument.legacy_id = trio.instrument_id
            union all
            select 'listed_as', 'instrument', instrument.component, 'listing', listing.component,
                   trio.known_at, trio.valid_from, trio.confidence, trio.raw_ref, trio.source,
                   instrument.reason like 'conflict:%' or listing.reason like 'conflict:%'
            from trio
            join instrument_component instrument on instrument.legacy_id = trio.instrument_id
            join listing_component listing on listing.legacy_id = trio.listing_id
        ), relation_claim as (
            select relation_type, from_kind, from_component, to_kind, to_component,
                   min(known_at) as known_at, min(valid_from) as valid_from, max(confidence) as confidence,
                   (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
                   (array_agg(source order by known_at, raw_ref))[1] as source,
                   bool_or(coalesce(blocked, false)) as blocked
            from relation_use group by 1, 2, 3, 4, 5
        ), relation_state as (
            select relation_claim.*,
                   count(*) over (partition by relation_type, to_kind, to_component) as claimants
            from relation_claim
        )
        select 'alias' as claim, kind, component, scheme, value, null::text as relation_type,
               null::text as to_kind, null::text as to_component, valid_from, known_at as transaction_time,
               source, raw_ref, method, confidence, evidence, legacy_id, link_state,
               case when link_state like 'conflict:%' then link_state else 'planned' end as status
        from alias_claim
        union all
        select 'relation', from_kind, from_component, null, null, relation_type, to_kind, to_component,
               valid_from, known_at, source, raw_ref, 'derived', confidence,
               jsonb_build_object('co_occurring_in', raw_ref), null, null,
               case
                   when blocked then 'conflict:endpoint-in-conflict'
                   when claimants > 1 and relation_type = 'issues' then 'conflict:instrument-claimed-by-several-issuers'
                   when claimants > 1 then 'conflict:listing-claimed-by-several-instruments'
                   else 'planned'
               end
        from relation_state$vc$;
    end if;

    if to_regclass('mart.entity_display_resolution') is null then
        execute $vc$create or replace view mart.entity_display_resolution as
         WITH primary_listings AS (
                 SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id AS issuer_id, relation.to_entity_id AS listing_id
                   FROM staging.entity_relations relation
                  WHERE relation.relation_type = 'primary_listing'::text AND NOT (EXISTS (SELECT 1 FROM staging.entity_retractions retraction WHERE retraction.relation_id = relation.relation_id))
                  ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
                ), entity_tickers AS (
                 SELECT DISTINCT ON (entity_aliases.entity_id) entity_aliases.entity_id,
                        CASE
                            WHEN entity_aliases.scheme = 'mic-ticker'::text THEN upper(split_part(entity_aliases.value, ':'::text, 2))
                            WHEN entity_aliases.scheme = 'legacy-id'::text AND entity_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(entity_aliases.value, ':'::text, 3))
                            ELSE NULL::text
                        END AS ticker
                   FROM staging.entity_aliases
                  WHERE entity_aliases.scheme = 'mic-ticker'::text AND NULLIF(btrim(split_part(entity_aliases.value, ':'::text, 2)), ''::text) IS NOT NULL OR entity_aliases.scheme = 'legacy-id'::text AND entity_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                  ORDER BY entity_aliases.entity_id, (entity_aliases.scheme = 'mic-ticker'::text) DESC, entity_aliases.confidence DESC, entity_aliases.transaction_time DESC, entity_aliases.alias_id DESC
                ), latest_members AS (
                 SELECT DISTINCT ON (m.issuer_id) m.issuer_id, m.listing_id,
                        CASE WHEN m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.listing_id::uuid ELSE NULL::uuid END AS listing_uuid,
                        CASE WHEN m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.issuer_id::uuid ELSE (SELECT ea.entity_id FROM staging.entity_aliases ea WHERE ea.scheme = 'legacy-id'::text AND ea.value = m.issuer_id ORDER BY ea.transaction_time DESC, ea.alias_id DESC LIMIT 1) END AS issuer_uuid
                   FROM staging.topt_core_snapshot_members m
                     LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id = CASE WHEN m.issuer_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.issuer_id::uuid ELSE NULL::uuid END
                      AND primary_listing.listing_id = CASE WHEN m.listing_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text THEN m.listing_id::uuid ELSE NULL::uuid END
                  ORDER BY m.issuer_id, (primary_listing.listing_id IS NOT NULL) DESC, m.created_at DESC, m.listing_id
                ), issuer_names AS (
                 SELECT DISTINCT ON (entity_aliases.entity_id) entity_aliases.entity_id, NULLIF(btrim(entity_aliases.value), ''::text) AS display_name
                   FROM staging.entity_aliases
                  WHERE entity_aliases.scheme = 'name'::text AND NULLIF(btrim(entity_aliases.value), ''::text) IS NOT NULL
                  ORDER BY entity_aliases.entity_id, entity_aliases.confidence DESC, entity_aliases.transaction_time DESC, entity_aliases.alias_id DESC
                ), constituent_names AS (
                 SELECT DISTINCT ON ((upper(cf.ticker))) upper(cf.ticker) AS ticker, btrim(cf.company_name) AS display_name
                   FROM staging.etf_constituent_facts cf
                  WHERE NULLIF(btrim(cf.company_name), ''::text) IS NOT NULL AND upper(btrim(cf.company_name)) <> upper(btrim(cf.ticker))
                  ORDER BY (upper(cf.ticker)), cf.knowable_at DESC, cf.as_of DESC, cf.id DESC
                ), resolved_tickers AS (
                 SELECT lm.issuer_id, lm.listing_id, lm.issuer_uuid,
                        COALESCE(lt.ticker, it.ticker, CASE WHEN lm.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(lm.listing_id, ':'::text, 3)) ELSE NULL::text END) AS ticker
                   FROM latest_members lm
                     LEFT JOIN entity_tickers lt ON lt.entity_id = lm.listing_uuid
                     LEFT JOIN entity_tickers it ON it.entity_id = lm.issuer_uuid
                )
         SELECT rt.issuer_id, rt.listing_id, rt.ticker,
            COALESCE(CASE WHEN upper(btrim(inam.display_name)) <> upper(btrim(COALESCE(rt.ticker, ''::text))) THEN NULLIF(btrim(inam.display_name), ''::text) ELSE NULL::text END, cn.display_name) AS display_name
           FROM resolved_tickers rt
             LEFT JOIN issuer_names inam ON inam.entity_id = rt.issuer_uuid
             LEFT JOIN constituent_names cn ON cn.ticker = upper(rt.ticker)
          ORDER BY rt.issuer_id$vc$;
    end if;

    if to_regclass('mart.entity_identity') is null then
        execute $vc$create or replace view mart.entity_identity as
         WITH RECURSIVE merge_edges AS (
                 SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id, relation.to_entity_id
                   FROM staging.entity_relations relation
                  WHERE relation.relation_type = 'superseded_by'::text AND NOT (EXISTS (SELECT 1 FROM staging.entity_retractions retraction WHERE retraction.relation_id = relation.relation_id))
                  ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
                ), survivor_walk(entity_id, current_id, depth) AS (
                 SELECT entity.entity_id, entity.entity_id, 0 FROM staging.entities entity
                UNION ALL
                 SELECT walk.entity_id, edge.to_entity_id, walk.depth + 1
                   FROM survivor_walk walk
                     JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
                  WHERE walk.depth < 16 AND edge.to_entity_id <> walk.entity_id
                ), survivors AS (
                 SELECT DISTINCT ON (walk.entity_id) walk.entity_id, walk.current_id AS id
                   FROM survivor_walk walk ORDER BY walk.entity_id, walk.depth DESC
                ), alias_retractions AS (
                 SELECT retraction.alias_id, min(retraction.valid_to) AS valid_to
                   FROM staging.entity_retractions retraction
                  WHERE retraction.alias_id IS NOT NULL GROUP BY retraction.alias_id
                ), active_aliases AS (
                 SELECT survivor_1.id AS survivor_id, alias.scheme, alias.value,
                    row_number() OVER (PARTITION BY survivor_1.id, alias.scheme ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC) AS rn
                   FROM staging.entity_aliases alias
                     JOIN survivors survivor_1 ON survivor_1.entity_id = alias.entity_id
                     LEFT JOIN alias_retractions retracted ON retracted.alias_id = alias.alias_id
                  WHERE LEAST(alias.valid_to, retracted.valid_to) IS NULL OR LEAST(alias.valid_to, retracted.valid_to) > alias.valid_from
                ), latest_by_scheme AS (
                 SELECT active_aliases.survivor_id, active_aliases.scheme, active_aliases.value
                   FROM active_aliases WHERE active_aliases.rn = 1
                ), direct_ciks AS (
                 SELECT latest_by_scheme.survivor_id, latest_by_scheme.value AS cik FROM latest_by_scheme WHERE latest_by_scheme.scheme = 'cik'::text
                ), direct_leis AS (
                 SELECT latest_by_scheme.survivor_id, latest_by_scheme.value AS lei FROM latest_by_scheme WHERE latest_by_scheme.scheme = 'lei'::text
                ), direct_tickers AS (
                 SELECT DISTINCT ON (latest_by_scheme.survivor_id) latest_by_scheme.survivor_id,
                        CASE
                            WHEN latest_by_scheme.scheme = 'mic-ticker'::text THEN split_part(latest_by_scheme.value, ':'::text, 2)
                            WHEN latest_by_scheme.scheme = 'legacy-id'::text AND latest_by_scheme.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(latest_by_scheme.value, ':'::text, 3))
                            WHEN latest_by_scheme.scheme = 'ticker'::text THEN upper(latest_by_scheme.value)
                            ELSE NULL::text
                        END AS ticker
                   FROM latest_by_scheme
                  WHERE latest_by_scheme.scheme = 'mic-ticker'::text
                     OR latest_by_scheme.scheme = 'legacy-id'::text AND latest_by_scheme.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                     OR latest_by_scheme.scheme = 'ticker'::text
                  ORDER BY latest_by_scheme.survivor_id, (latest_by_scheme.scheme = 'mic-ticker'::text) DESC, (latest_by_scheme.scheme = 'legacy-id'::text) DESC
                ), primary_listings AS (
                 SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id AS issuer_id, relation.to_entity_id AS listing_id
                   FROM staging.entity_relations relation
                  WHERE relation.relation_type = 'primary_listing'::text AND NOT (EXISTS (SELECT 1 FROM staging.entity_retractions retraction WHERE retraction.relation_id = relation.relation_id))
                  ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
                ), issuer_tickers AS (
                 SELECT DISTINCT ON (r_issues.from_entity_id) r_issues.from_entity_id AS issuer_id, dt_1.ticker
                   FROM staging.entity_relations r_issues
                     JOIN staging.entity_relations r_listed ON r_listed.from_entity_id = r_issues.to_entity_id AND r_listed.relation_type = 'listed_as'::text
                     JOIN direct_tickers dt_1 ON dt_1.survivor_id = r_listed.to_entity_id
                     LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id = r_issues.from_entity_id AND primary_listing.listing_id = r_listed.to_entity_id
                  WHERE r_issues.relation_type = 'issues'::text AND dt_1.ticker IS NOT NULL
                  ORDER BY r_issues.from_entity_id, (primary_listing.listing_id IS NOT NULL) DESC, r_issues.transaction_time DESC, dt_1.ticker, r_issues.relation_id DESC
                ), instrument_tickers AS (
                 SELECT DISTINCT ON (r_listed.from_entity_id) r_listed.from_entity_id AS instrument_id, dt_1.ticker
                   FROM staging.entity_relations r_listed
                     JOIN direct_tickers dt_1 ON dt_1.survivor_id = r_listed.to_entity_id
                  WHERE r_listed.relation_type = 'listed_as'::text AND dt_1.ticker IS NOT NULL
                  ORDER BY r_listed.from_entity_id, r_listed.transaction_time DESC, dt_1.ticker, r_listed.relation_id DESC
                ), direct_names AS (
                 SELECT latest_by_scheme.survivor_id, latest_by_scheme.value AS name FROM latest_by_scheme WHERE latest_by_scheme.scheme = 'name'::text
                ), legacy_aliases AS (
                 SELECT DISTINCT ON (latest_by_scheme.survivor_id) latest_by_scheme.survivor_id, latest_by_scheme.value AS legacy_id
                   FROM latest_by_scheme WHERE latest_by_scheme.scheme = 'legacy-id'::text ORDER BY latest_by_scheme.survivor_id
                ), direct_listings AS (
                 SELECT DISTINCT ON (latest_by_scheme.survivor_id) latest_by_scheme.survivor_id,
                        CASE
                            WHEN latest_by_scheme.scheme = 'legacy-id'::text AND latest_by_scheme.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN latest_by_scheme.value
                            WHEN latest_by_scheme.scheme = 'mic-ticker'::text THEN (('listing:'::text || lower(split_part(latest_by_scheme.value, ':'::text, 1))) || ':'::text) || lower(split_part(latest_by_scheme.value, ':'::text, 2))
                            ELSE NULL::text
                        END AS listing_id
                   FROM latest_by_scheme WHERE latest_by_scheme.scheme = 'legacy-id'::text OR latest_by_scheme.scheme = 'mic-ticker'::text
                  ORDER BY latest_by_scheme.survivor_id, (latest_by_scheme.scheme = 'legacy-id'::text) DESC
                ), issuer_listings AS (
                 SELECT DISTINCT ON (r_issues.from_entity_id) r_issues.from_entity_id AS issuer_id, dl_1.listing_id
                   FROM staging.entity_relations r_issues
                     JOIN staging.entity_relations r_listed ON r_listed.from_entity_id = r_issues.to_entity_id AND r_listed.relation_type = 'listed_as'::text
                     JOIN direct_listings dl_1 ON dl_1.survivor_id = r_listed.to_entity_id
                     LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id = r_issues.from_entity_id AND primary_listing.listing_id = r_listed.to_entity_id
                  WHERE r_issues.relation_type = 'issues'::text AND dl_1.listing_id IS NOT NULL
                  ORDER BY r_issues.from_entity_id, (primary_listing.listing_id IS NOT NULL) DESC, r_issues.transaction_time DESC, dl_1.listing_id, r_issues.relation_id DESC
                )
         SELECT e.entity_id, e.kind, COALESCE(dt.ticker, it.ticker, inst.ticker) AS current_ticker,
            CASE WHEN upper(btrim(dn.name)) <> upper(btrim(COALESCE(dt.ticker, it.ticker, inst.ticker, ''::text))) THEN NULLIF(btrim(dn.name), ''::text) ELSE NULL::text END AS name, dc.cik, dl.lei, COALESCE(dlist.listing_id, ilist.listing_id) AS listing_id, la.legacy_id
           FROM staging.entities e
             JOIN survivors survivor ON survivor.entity_id = e.entity_id
             LEFT JOIN direct_tickers dt ON dt.survivor_id = survivor.id
             LEFT JOIN issuer_tickers it ON it.issuer_id = survivor.id
             LEFT JOIN instrument_tickers inst ON inst.instrument_id = survivor.id
             LEFT JOIN direct_names dn ON dn.survivor_id = survivor.id
             LEFT JOIN legacy_aliases la ON la.survivor_id = survivor.id
             LEFT JOIN direct_ciks dc ON dc.survivor_id = survivor.id
             LEFT JOIN direct_leis dl ON dl.survivor_id = survivor.id
             LEFT JOIN direct_listings dlist ON dlist.survivor_id = survivor.id
             LEFT JOIN issuer_listings ilist ON ilist.issuer_id = survivor.id$vc$;
    end if;

    if to_regclass('mart.fund_holdings') is null then
        execute $vc$create or replace view mart.fund_holdings as
         SELECT DISTINCT ON (facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name) facts.fund_id::text AS fund_id,
            fund_name.value AS fund_name, facts.holding_id::text AS holding_id, facts.holding_name, facts.isin,
            facts.report_period, facts.transaction_time, facts.percent_of_net_assets, facts.value_usd,
            facts.balance, facts.confidence, facts.raw_ref
           FROM staging.fund_holding_facts facts
             LEFT JOIN LATERAL ( SELECT alias.value FROM staging.entity_aliases alias WHERE alias.entity_id = facts.fund_id AND alias.scheme = 'name'::text ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC LIMIT 1) fund_name ON true
          ORDER BY facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name, facts.recorded_at DESC, facts.id DESC$vc$;
    end if;

    if to_regclass('mart.fund_holdings_resolved') is null then
        execute $vc$create or replace view mart.fund_holdings_resolved as
         WITH RECURSIVE merge_edges AS (
                 SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id, relation.to_entity_id
                   FROM staging.entity_relations relation
                  WHERE relation.relation_type = 'superseded_by'::text
                    AND NOT (EXISTS (SELECT 1 FROM staging.entity_retractions retraction WHERE retraction.relation_id = relation.relation_id))
                  ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
                ), survivor_walk(entity_id, current_id, depth) AS (
                 SELECT entity.entity_id, entity.entity_id, 0 AS depth FROM staging.entities entity
                UNION ALL
                 SELECT walk.entity_id, edge.to_entity_id, walk.depth + 1
                   FROM survivor_walk walk JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
                  WHERE walk.depth < 32
                ), survivors AS (
                 SELECT DISTINCT ON (walk.entity_id) walk.entity_id, walk.current_id AS survivor_id
                   FROM survivor_walk walk ORDER BY walk.entity_id, walk.depth DESC
                ), resolved AS (
                 SELECT holdings.fund_id, holdings.fund_name, holdings.holding_id, holdings.holding_name, holdings.isin,
                    holdings.report_period, holdings.transaction_time, holdings.percent_of_net_assets,
                    holdings.value_usd, holdings.balance, holdings.confidence, holdings.raw_ref,
                    COALESCE(
                        NULLIF(holdings.holding_id, ''::text)::uuid,
                        (SELECT alias.entity_id FROM staging.entity_aliases alias WHERE alias.scheme = 'isin'::text AND alias.value = upper(holdings.isin) ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC LIMIT 1)
                    ) AS holding_uuid
                   FROM mart.fund_holdings holdings
                ), linked AS (
                 SELECT resolved.*,
                    COALESCE(
                        (SELECT COALESCE(sup.survivor_id, rel.from_entity_id) FROM staging.entity_relations rel LEFT JOIN survivors sup ON sup.entity_id = rel.from_entity_id WHERE rel.to_entity_id = resolved.holding_uuid AND rel.relation_type = 'issues'::text ORDER BY rel.transaction_time DESC, rel.relation_id DESC LIMIT 1),
                        COALESCE(self_sup.survivor_id, resolved.holding_uuid)
                    ) AS issuer_uuid
                   FROM resolved LEFT JOIN survivors self_sup ON self_sup.entity_id = resolved.holding_uuid
                )
         SELECT linked.fund_id, linked.fund_name, linked.report_period, linked.transaction_time,
            linked.holding_name, linked.isin, linked.percent_of_net_assets, linked.value_usd,
            linked.issuer_uuid::text AS issuer_entity,
            COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) AS ticker,
            COALESCE(
                listing_alias.listing_id,
                CASE WHEN COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) IS NOT NULL THEN 'listing:xnas:'::text || lower(COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value)) ELSE NULL::text END
            ) AS listing_id
           FROM linked
             LEFT JOIN LATERAL (
                 SELECT
                    CASE
                        WHEN alias.scheme = 'mic-ticker'::text THEN upper(split_part(alias.value, ':'::text, 2))
                        WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(alias.value, ':'::text, 3))
                        WHEN alias.scheme = 'ticker'::text THEN upper(alias.value)
                        ELSE NULL::text
                    END AS ticker,
                    CASE
                        WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN alias.value
                        WHEN alias.scheme = 'mic-ticker'::text THEN ('listing:'::text || lower(split_part(alias.value, ':'::text, 1))) || ':'::text || lower(split_part(alias.value, ':'::text, 2))
                        ELSE NULL::text
                    END AS listing_id
                 FROM staging.entity_relations rel
                 JOIN staging.entity_aliases alias ON alias.entity_id = rel.to_entity_id
                 WHERE rel.from_entity_id = linked.holding_uuid AND rel.relation_type = 'listed_as'::text
                   AND (alias.scheme = 'mic-ticker'::text OR alias.scheme = 'ticker'::text OR (alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text))
                 ORDER BY (alias.scheme = 'mic-ticker'::text) DESC, (alias.scheme = 'legacy-id'::text) DESC, alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC
                 LIMIT 1
             ) listing_alias ON true
             LEFT JOIN LATERAL (
                 SELECT upper(alias.value) AS value FROM staging.entity_aliases alias
                 WHERE alias.entity_id = linked.holding_uuid AND alias.scheme = 'ticker'::text
                 ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC LIMIT 1
             ) holding_ticker ON true
             LEFT JOIN LATERAL (
                 SELECT upper(alias.value) AS value FROM staging.entity_aliases alias
                 WHERE alias.entity_id = linked.issuer_uuid AND alias.scheme = 'ticker'::text
                 ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC LIMIT 1
             ) issuer_ticker ON true$vc$;
    end if;

    if to_regclass('mart.fund_holdings_coverage') is null then
        execute $vc$create or replace view mart.fund_holdings_coverage as
         SELECT fund_holdings_resolved.fund_id, fund_holdings_resolved.report_period,
            fund_holdings_resolved.transaction_time, count(*) AS lines,
            count(*) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL) AS resolved_lines,
            COALESCE(sum(fund_holdings_resolved.percent_of_net_assets), 0::numeric) AS total_weight_pct,
            COALESCE(sum(fund_holdings_resolved.percent_of_net_assets) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL), 0::numeric) AS resolved_weight_pct
           FROM mart.fund_holdings_resolved
          GROUP BY fund_holdings_resolved.fund_id, fund_holdings_resolved.report_period, fund_holdings_resolved.transaction_time$vc$;
    end if;

    if to_regclass('mart.fund_holdings_valuation') is null then
        execute $vc$create or replace view mart.fund_holdings_valuation as
         WITH newest AS (
                 SELECT DISTINCT ON (fund_holdings.fund_id) fund_holdings.fund_id, fund_holdings.report_period, fund_holdings.transaction_time
                   FROM mart.fund_holdings ORDER BY fund_holdings.fund_id, fund_holdings.report_period DESC, fund_holdings.transaction_time DESC
                )
         SELECT resolved.fund_id, resolved.fund_name, resolved.report_period, resolved.transaction_time,
            resolved.holding_name, resolved.isin, resolved.percent_of_net_assets, resolved.value_usd,
            resolved.issuer_entity, resolved.ticker, resolved.listing_id
           FROM mart.fund_holdings_resolved resolved
             JOIN newest USING (fund_id, report_period, transaction_time)$vc$;
    end if;
end
$$;
