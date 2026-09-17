-- #877: the entity backfill's definitions, schema only.
--
-- 20260917T0430 used to define these AND run the backfill on every boot, which held
-- llm-service's boot for 34 s on staging and failed the v0.0.80 rollout. That file is
-- now empty; the definitions live here and nothing here reads or writes data. The
-- Dagster job `entity_identity_backfill` (data_engine.lanes.entity_identity) calls
-- staging.entity_backfill(); its sensor decides when, with the two cheap reads at the
-- end of this file.
--
--   staging.entity_backfill_plan       the whole plan as one SELECT over tables that
--                                      existed before the entity migrations (the dry run)
--   staging.entity_backfill()          applies the plan; idempotent; deterministic ids
--   staging.entity_evidence_watermark() the newest recorded row among the backfill's inputs
--   staging.entity_backfill_due()      why a backfill is due, or null
--
-- `create or replace` only: a boot re-applies this file in milliseconds and takes no lock
-- on any table the pipeline writes.

-- The plan, as one SELECT over tables that existed before either entity migration, so it can
-- be run read-only against any environment before deploy (the #877 PR-2 dry run is this
-- SELECT).
-- `valid_from` is the earliest valid date of the evidence; the apply step widens it to
-- '-infinity' for schemes whose values never change hands. One row per claim:
--   claim = 'alias'    -> (kind, component) holds scheme:value
--   claim = 'relation' -> (kind, component) relation_type (to_kind, to_component)
-- A component is one real-world entity as the stored evidence proves it; the apply step
-- maps it to an existing entity or mints one. `status` is 'planned' or why a claim is held
-- back; `link_state` says, for each legacy id, whether it was joined across schemes.
--
-- What counts as a use: every (issuer, instrument, listing) trio in a stored normalized
-- observation payload, and every trio in a published universe head (the plane's governed
-- corpus). Snapshot members are frozen from those observations and add nothing. The
-- packaged TOPT corpus reaches the database only through its observations; all 21 of its
-- trios are observed in both environments.
--
-- What counts as a cross-scheme proof (issuers): an N-PORT holding line that carries the
-- LEI and an ISIN, and the knowledge graph resolving that ISIN (newest identifier vintage,
-- one same_as hop, exactly as factors.shared.entity_resolution.resolve) to a CIK-keyed
-- entity. Every line for the LEI must agree on one CIK and no other LEI may prove the same
-- CIK. Instruments: a CUSIP-keyed trio and a FIGI-keyed trio name the same listing, their
-- issuers are the same proven component, and an N-PORT line with that CUSIP resolves to
-- the same CIK; one FIGI per CUSIP and one CUSIP per FIGI.
create or replace view staging.entity_backfill_plan as
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
           -- A published head is a frozen release projection; the capture grades the
           -- identity it derives from one as exact (release_derived_adapter), and so does this.
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
           lpad(substring(coalesce(hop.to_id, identifier.entity_id)
                          from '^(?:issuer|company):cik:([0-9]{1,10})$'), 10, '0') as cik,
           greatest(line.transaction_time, identifier.transaction_time, hop.transaction_time) as known_at,
           least(line.confidence, identifier.confidence, hop.confidence) as confidence,
           line.raw_ref,
           jsonb_build_object(
               'fund_holding_fact_id', line.id,
               'nport_raw_ref', line.raw_ref,
               'kg_identifier_id', identifier.id,
               'kg_identifier_raw_ref', identifier.raw_ref,
               'kg_same_as_edge_id', hop.id,
               'kg_entity_id', coalesce(hop.to_id, identifier.entity_id)) as evidence
    from staging.fund_holding_facts line
    cross join lateral (
        select kg.id, kg.entity_id, kg.transaction_time, kg.confidence, kg.raw_ref
        from staging.kg_identifiers kg
        where kg.identifier_type = 'isin' and kg.identifier_value = line.isin
        order by kg.transaction_time desc, kg.confidence desc, kg.id desc
        limit 1
    ) identifier
    left join lateral (
        select edge.id, edge.to_id, edge.transaction_time, edge.confidence
        from staging.kg_edges edge
        where edge.from_id = identifier.entity_id and edge.relation_type = 'same_as'
        order by edge.transaction_time desc, edge.id desc
        limit 1
    ) hop on true
    where line.lei ~ '^[0-9A-Za-z]{20}$'
      and line.isin ~ '^[0-9A-Za-z]{12}$'
      and coalesce(hop.to_id, identifier.entity_id) ~ '^(issuer|company):cik:[0-9]{1,10}$'
), lei_link as (
    select lei,
           count(distinct cik) as ciks,
           min(cik) as cik,
           min(report_period) as valid_from,
           (array_agg(known_at order by known_at, line_id))[1] as known_at,
           (array_agg(confidence order by known_at, line_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, line_id))[1] as evidence
    from proof
    group by lei
), cik_leis as (
    select cik, count(distinct lei) as leis from proof group by cik
), cusip_isin as (
    select cusip,
           count(distinct isin) as isins,
           count(distinct cik) as ciks,
           min(isin) as isin,
           min(cik) as cik,
           min(report_period) as valid_from,
           (array_agg(known_at order by known_at, line_id))[1] as known_at,
           (array_agg(confidence order by known_at, line_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, line_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, line_id))[1] as evidence
    from proof
    where cusip ~ '^[0-9A-Z*@#]{9}$'
    group by cusip
), isin_cusips as (
    select isin, count(distinct cusip) as cusips from proof where cusip is not null group by isin
), issuer_component as (
    select parsed.legacy_id,
           case
               when parsed.roles > 1 then 'issuer/legacy=' || parsed.legacy_id
               when parsed.scheme = 'cik' then 'issuer/cik=' || parsed.value
               when parsed.scheme = 'lei' and lei_link.ciks = 1 and cik_leis.leis = 1
                   then 'issuer/cik=' || lei_link.cik
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
    select cusip_trio.instrument_id as cusip_id,
           figi_trio.instrument_id as figi_id,
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
    join trio figi_trio
      on figi_trio.listing_id = cusip_trio.listing_id
    join parsed cusip_parsed
      on cusip_parsed.role = 'instrument' and cusip_parsed.legacy_id = cusip_trio.instrument_id
     and cusip_parsed.scheme = 'cusip'
    join parsed figi_parsed
      on figi_parsed.role = 'instrument' and figi_parsed.legacy_id = figi_trio.instrument_id
     and figi_parsed.scheme = 'figi'
    join issuer_component cusip_issuer on cusip_issuer.legacy_id = cusip_trio.issuer_id
    join issuer_component figi_issuer on figi_issuer.legacy_id = figi_trio.issuer_id
    join cusip_isin on cusip_isin.cusip = cusip_parsed.value
    where cusip_issuer.component = figi_issuer.component
      and cusip_issuer.component like 'issuer/cik=%'
      and cusip_isin.ciks = 1
      and 'issuer/cik=' || cusip_isin.cik = cusip_issuer.component
), cusip_link as (
    select cusip_id,
           count(distinct figi_id) as figis,
           min(figi_id) as figi_id,
           (array_agg(known_at order by known_at, figi_id))[1] as known_at,
           (array_agg(confidence order by known_at, figi_id))[1] as confidence,
           (array_agg(raw_ref order by known_at, figi_id))[1] as raw_ref,
           (array_agg(evidence order by known_at, figi_id))[1] as evidence
    from pair
    group by cusip_id
), figi_cusips as (
    select figi_id, count(distinct cusip_id) as cusips from pair group by figi_id
), instrument_component as (
    select parsed.legacy_id,
           case
               when parsed.roles > 1 then 'instrument/legacy=' || parsed.legacy_id
               when parsed.scheme = 'figi' then 'instrument/figi=' || parsed.value
               when parsed.scheme = 'cusip' and cusip_link.figis = 1 and figi_cusips.cusips = 1
                   then 'instrument/figi=' || figi_parsed.value
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
    from parsed
    where parsed.role = 'listing'
), component as (
    select 'issuer' as kind, legacy_id, component, reason from issuer_component
    union all
    select 'instrument', legacy_id, component, reason from instrument_component
    union all
    select 'listing', legacy_id, component, reason from listing_component
), component_state as (
    select component.*,
           count(*) over (partition by component.kind, component.component) as legacy_ids,
           parsed.scheme,
           parsed.value,
           parsed.known_at,
           parsed.valid_from,
           parsed.raw_ref,
           parsed.source,
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
    -- the legacy id itself
    select kind, component, 'legacy-id' as scheme, legacy_id as value,
           valid_from, known_at, source, raw_ref,
           'asserted' as method, 1.0 as confidence,
           jsonb_build_object('used_by', source) as evidence,
           legacy_id, link_state
    from component_state
    union all
    -- the typed id the legacy id spells out
    select kind, component, scheme, value,
           valid_from, known_at, source, raw_ref,
           'parsed', 1.0,
           jsonb_build_object('parsed_from', legacy_id),
           legacy_id, link_state
    from component_state
    where scheme is not null
    union all
    -- the CIK an LEI-keyed issuer was proven to hold
    select state.kind, state.component, 'cik', lei_link.cik,
           lei_link.valid_from, lei_link.known_at, 'nport+kg', lei_link.raw_ref,
           'crosswalk', lei_link.confidence,
           lei_link.evidence || jsonb_build_object('lei', state.value),
           state.legacy_id, state.link_state
    from component_state state
    join lei_link on lei_link.lei = state.value
    where state.kind = 'issuer' and state.scheme = 'lei'
      and state.component = 'issuer/cik=' || lei_link.cik
    union all
    -- the ISIN on the N-PORT line of a CUSIP-keyed instrument
    select state.kind, state.component, 'isin', cusip_isin.isin,
           cusip_isin.valid_from, cusip_isin.known_at, 'nport+kg', cusip_isin.raw_ref,
           'crosswalk', cusip_isin.confidence,
           cusip_isin.evidence || jsonb_build_object('cusip', state.value),
           state.legacy_id, state.link_state
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
             cusip_isin.raw_ref, cusip_isin.confidence, cusip_isin.evidence, state.value,
             state.legacy_id, state.link_state
    union all
    -- the evidence that joined a CUSIP-keyed and a FIGI-keyed instrument
    select state.kind, state.component, 'cusip', state.value,
           state.valid_from, cusip_link.known_at, 'shared-listing+nport+kg', cusip_link.raw_ref,
           'crosswalk', cusip_link.confidence,
           cusip_link.evidence || jsonb_build_object('figi_legacy_id', cusip_link.figi_id),
           state.legacy_id, state.link_state
    from component_state state
    join cusip_link on cusip_link.cusip_id = state.legacy_id
    where state.kind = 'instrument' and state.scheme = 'cusip'
      and state.component like 'instrument/figi=%'
), relation_use as (
    select 'issues' as relation_type,
           'issuer' as from_kind, issuer.component as from_component,
           'instrument' as to_kind, instrument.component as to_component,
           trio.known_at, trio.valid_from, trio.confidence, trio.raw_ref, trio.source,
           issuer.reason like 'conflict:%' or instrument.reason like 'conflict:%' as blocked
    from trio
    join issuer_component issuer on issuer.legacy_id = trio.issuer_id
    join instrument_component instrument on instrument.legacy_id = trio.instrument_id
    union all
    select 'listed_as',
           'instrument', instrument.component,
           'listing', listing.component,
           trio.known_at, trio.valid_from, trio.confidence, trio.raw_ref, trio.source,
           instrument.reason like 'conflict:%' or listing.reason like 'conflict:%'
    from trio
    join instrument_component instrument on instrument.legacy_id = trio.instrument_id
    join listing_component listing on listing.legacy_id = trio.listing_id
), relation_claim as (
    select relation_type, from_kind, from_component, to_kind, to_component,
           min(known_at) as known_at,
           min(valid_from) as valid_from,
           max(confidence) as confidence,
           (array_agg(raw_ref order by known_at, raw_ref))[1] as raw_ref,
           (array_agg(source order by known_at, raw_ref))[1] as source,
           bool_or(coalesce(blocked, false)) as blocked
    from relation_use
    group by 1, 2, 3, 4, 5
), relation_state as (
    select relation_claim.*,
           -- An instrument has one issuer and a listing one instrument: two different
           -- components on the "one" side means the ids were not joined, and neither edge is
           -- written until they are.
           count(*) over (partition by relation_type, to_kind, to_component) as claimants
    from relation_claim
)
select 'alias' as claim,
       kind,
       component,
       scheme,
       value,
       null::text as relation_type,
       null::text as to_kind,
       null::text as to_component,
       valid_from,
       known_at as transaction_time,
       source,
       raw_ref,
       method,
       confidence,
       evidence,
       legacy_id,
       link_state,
       case when link_state like 'conflict:%' then link_state else 'planned' end as status
from alias_claim
union all
select 'relation',
       from_kind,
       from_component,
       null,
       null,
       relation_type,
       to_kind,
       to_component,
       valid_from,
       known_at,
       source,
       raw_ref,
       'derived',
       confidence,
       jsonb_build_object('co_occurring_in', raw_ref),
       null,
       null,
       case
           when blocked then 'conflict:endpoint-in-conflict'
           when claimants > 1 and relation_type = 'issues' then 'conflict:instrument-claimed-by-several-issuers'
           when claimants > 1 then 'conflict:listing-claimed-by-several-instruments'
           else 'planned'
       end
from relation_state;

comment on view staging.entity_backfill_plan is
    '#877 PR-2: the entity backfill as one read-only SELECT over pre-existing tables. '
    'staging.entity_backfill() applies it.';

-- Apply the plan. Idempotent: a claim already held by the entity it maps to (same scheme,
-- value, method and source, validity covered) is skipped, and a component that already has
-- an entity reuses it. A component the evidence now proves to span two existing entities is
-- merged: the entity born from the component's birth alias survives, and each other one
-- gets `same_as` and `superseded_by` edges to it.
-- Nothing is updated. Values the registry refuses are held back; a component that cannot be
-- written (its aliases already name another kind, or a guard refuses a row) is rolled back
-- on its own and reported. This runs on every boot and must never crash-loop a service on
-- data it did not expect; what it held back is in the returned summary, which the boot log
-- prints.
create or replace function staging.entity_backfill()
returns jsonb
language plpgsql
-- JIT compiles again on every execution; on staging it cost 0.45 s on the plan query alone.
set jit = off
as $$
declare
    v_version constant text := 'entity-backfill:v1';
    v_started timestamptz := clock_timestamp();
    v_component record;
    v_claim record;
    v_birth_scheme text;
    v_birth_value text;
    v_existing uuid[];
    v_survivor uuid;
    v_loser uuid;
    v_merge_known timestamptz;
    v_merge_evidence jsonb;
    v_from uuid;
    v_to uuid;
    v_failed jsonb := '[]'::jsonb;
begin
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_backfill', 0));

    if to_regclass('pg_temp.entity_backfill_claims') is not null then
        drop table pg_temp.entity_backfill_claims;
    end if;
    create temporary table entity_backfill_claims on commit drop as
        select * from staging.entity_backfill_plan;

    -- The registry, not the plan, knows which values are well formed and which schemes
    -- never change hands (those hold for all time once assigned).
    update pg_temp.entity_backfill_claims claim
       set status = case
               when claim.value is distinct from staging.entity_alias_normalize(claim.scheme, claim.value)
                 or claim.value !~ scheme.value_pattern
                 or not staging.entity_kind_is_a(claim.kind, scheme.applies_to_kind)
               then 'skipped:invalid-value'
               else claim.status
           end,
           valid_from = case when scheme.value_reusable then claim.valid_from else '-infinity'::date end
      from staging.entity_alias_schemes scheme
     where claim.claim = 'alias'
       and claim.status = 'planned'
       and scheme.scheme = claim.scheme;

    if to_regclass('pg_temp.entity_backfill_components') is not null then
        drop table pg_temp.entity_backfill_components;
    end if;
    create temporary table entity_backfill_components (
        kind text not null,
        component text not null,
        entity_id uuid not null,
        primary key (kind, component)
    ) on commit drop;

    for v_component in
        select kind, component
        from pg_temp.entity_backfill_claims
        where claim = 'alias' and status = 'planned'
        group by kind, component
        order by kind, component
    loop
        begin
            v_birth_scheme := null;
            v_birth_value := null;

            -- Entities that already hold one of this component's unique aliases over an
            -- overlapping validity, as the entities they survive as.
            select array_agg(distinct staging.entity_survivor(alias.entity_id, 'infinity'))
              into v_existing
            from pg_temp.entity_backfill_claims claim
            join staging.entity_alias_schemes scheme
              on scheme.scheme = claim.scheme and scheme.is_unique
            join staging.entity_aliases alias
              on alias.scheme = claim.scheme and alias.value = claim.value
            where claim.claim = 'alias' and claim.status = 'planned'
              and claim.kind = v_component.kind and claim.component = v_component.component
              and alias.valid_from
                  < coalesce(staging.entity_alias_valid_to(alias.alias_id, 'infinity'), 'infinity'::date)
              and claim.valid_from
                  < coalesce(staging.entity_alias_valid_to(alias.alias_id, 'infinity'), 'infinity'::date);

            if exists (
                select 1 from staging.entities entity
                where entity.entity_id = any(coalesce(v_existing, '{}'::uuid[]))
                  and entity.kind <> v_component.kind
            ) then
                raise exception 'aliases of % already name an entity of another kind', v_component.component;
            end if;

            -- The birth alias comes from the component's claims alone, never from the clock
            -- or the load order: the pipeline's own legacy id that became knowable first,
            -- then the smallest value. Evidence that arrives later is almost always knowable
            -- later too, so a store that grows run by run picks the birth a store loaded at
            -- once picks.
            select claim.scheme, claim.value into v_birth_scheme, v_birth_value
            from pg_temp.entity_backfill_claims claim
            where claim.claim = 'alias' and claim.status = 'planned'
              and claim.kind = v_component.kind and claim.component = v_component.component
            order by (claim.scheme = 'legacy-id') desc, claim.transaction_time, claim.scheme, claim.value
            limit 1;

            if v_existing is null then
                v_survivor := staging.entity_mint(v_component.kind, v_birth_scheme, v_birth_value, v_version);
            else
                -- The survivor follows the same rule: the entity born from the component's
                -- birth alias, else the one whose birth alias became knowable first. A store
                -- that learned the link late then resolves every alias to the entity a store
                -- that knew it from the start minted.
                select entity.entity_id into v_survivor
                from staging.entities entity
                where entity.entity_id = any(v_existing)
                order by (entity.birth_scheme = v_birth_scheme and entity.birth_value = v_birth_value) desc,
                         (entity.birth_scheme = 'legacy-id') desc,
                         (select min(birth.transaction_time) from staging.entity_aliases birth
                          where birth.entity_id = entity.entity_id
                            and birth.scheme = entity.birth_scheme and birth.value = entity.birth_value),
                         entity.birth_scheme, entity.birth_value, entity.birth_generation
                limit 1;
                if cardinality(v_existing) > 1 then
                    select max(claim.transaction_time),
                           jsonb_build_object(
                               'component', v_component.component,
                               'crosswalk', coalesce(
                                   jsonb_agg(claim.evidence) filter (where claim.method = 'crosswalk'),
                                   '[]'::jsonb))
                      into v_merge_known, v_merge_evidence
                    from pg_temp.entity_backfill_claims claim
                    where claim.claim = 'alias' and claim.status = 'planned'
                      and claim.kind = v_component.kind and claim.component = v_component.component;
                    for v_loser in
                        select entity.entity_id from staging.entities entity
                        where entity.entity_id = any(v_existing) and entity.entity_id <> v_survivor
                        order by entity.birth_scheme, entity.birth_value, entity.birth_generation
                    loop
                        insert into staging.entity_relations
                            (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                             transaction_time, source, raw_ref, method, confidence, evidence,
                             mapping_version)
                        select staging.entity_relation_uuid(
                                   edge.relation_type, v_loser, v_survivor, '-infinity',
                                   v_merge_known, 'entity-backfill', 'merge'),
                               edge.relation_type, v_loser, v_survivor, '-infinity',
                               v_merge_known, 'entity-backfill', v_component.component, 'merge', 1.0,
                               v_merge_evidence, v_version
                        from (values ('same_as'), ('superseded_by')) as edge (relation_type);
                    end loop;
                end if;
            end if;

            for v_claim in
                select * from pg_temp.entity_backfill_claims claim
                where claim.claim = 'alias' and claim.status = 'planned'
                  and claim.kind = v_component.kind and claim.component = v_component.component
                order by (claim.scheme = v_birth_scheme and claim.value = v_birth_value) desc nulls last,
                         claim.transaction_time, claim.scheme, claim.value, claim.method, claim.source
            loop
                if exists (
                    select 1 from staging.entity_aliases alias
                    where alias.scheme = v_claim.scheme and alias.value = v_claim.value
                      and alias.method = v_claim.method and alias.source = v_claim.source
                      and alias.valid_from <= v_claim.valid_from
                      and staging.entity_alias_valid_to(alias.alias_id, 'infinity') is null
                      and staging.entity_survivor(alias.entity_id, 'infinity') = v_survivor
                ) then
                    continue;
                end if;
                insert into staging.entity_aliases
                    (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,
                     method, confidence, evidence, mapping_version)
                values
                    (v_survivor, v_claim.scheme, v_claim.value, v_claim.valid_from,
                     v_claim.transaction_time, v_claim.source, v_claim.raw_ref, v_claim.method,
                     v_claim.confidence, v_claim.evidence, v_version);
            end loop;

            insert into pg_temp.entity_backfill_components (kind, component, entity_id)
            values (v_component.kind, v_component.component, v_survivor);
        exception when others then
            v_failed := v_failed || jsonb_build_object(
                'component', v_component.component, 'kind', v_component.kind, 'error', sqlerrm);
        end;
    end loop;

    for v_claim in
        select * from pg_temp.entity_backfill_claims claim
        where claim.claim = 'relation' and claim.status = 'planned'
        order by claim.relation_type, claim.transaction_time, claim.component, claim.to_component
    loop
        select entity_id into v_from from pg_temp.entity_backfill_components
        where kind = v_claim.kind and component = v_claim.component;
        select entity_id into v_to from pg_temp.entity_backfill_components
        where kind = v_claim.to_kind and component = v_claim.to_component;
        if v_from is null or v_to is null then
            update pg_temp.entity_backfill_claims
               set status = 'skipped:endpoint-not-written'
             where claim = 'relation' and relation_type = v_claim.relation_type
               and component = v_claim.component and to_component = v_claim.to_component;
            continue;
        end if;
        -- One issuer per instrument and one instrument per listing, across the whole store:
        -- another endpoint blocks the claim only while its edge is still in force on or after
        -- the claim's first valid day (the claims here are open-ended). An edge retracted
        -- before then is history, and one withdrawn outright never held.
        if exists (
            select 1
            from staging.entity_relations relation
            cross join lateral (
                select coalesce(staging.entity_relation_valid_to(relation.relation_id, 'infinity'),
                                'infinity'::date) as effective_to
            ) held
            where relation.relation_type = v_claim.relation_type
              and staging.entity_survivor(relation.to_entity_id, 'infinity') = v_to
              and staging.entity_survivor(relation.from_entity_id, 'infinity') <> v_from
              and relation.valid_from < held.effective_to
              and v_claim.valid_from < held.effective_to
        ) then
            update pg_temp.entity_backfill_claims
               set status = 'skipped:store-holds-another-endpoint'
             where claim = 'relation' and relation_type = v_claim.relation_type
               and component = v_claim.component and to_component = v_claim.to_component;
            continue;
        end if;
        if exists (
            select 1 from staging.entity_relations relation
            where relation.relation_type = v_claim.relation_type
              and staging.entity_survivor(relation.from_entity_id, 'infinity') = v_from
              and staging.entity_survivor(relation.to_entity_id, 'infinity') = v_to
              and relation.method = v_claim.method and relation.source = v_claim.source
              and relation.valid_from <= v_claim.valid_from
              and staging.entity_relation_valid_to(relation.relation_id, 'infinity') is null
        ) then
            continue;
        end if;
        begin
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
                 transaction_time, source, raw_ref, method, confidence, evidence, mapping_version)
            values
                (staging.entity_relation_uuid(
                     v_claim.relation_type, v_from, v_to, v_claim.valid_from,
                     v_claim.transaction_time, v_claim.source, v_claim.method),
                 v_claim.relation_type, v_from, v_to, v_claim.valid_from,
                 v_claim.transaction_time, v_claim.source, v_claim.raw_ref, v_claim.method,
                 v_claim.confidence, v_claim.evidence, v_version);
        exception when others then
            v_failed := v_failed || jsonb_build_object(
                'relation', v_claim.relation_type, 'from', v_claim.component,
                'to', v_claim.to_component, 'error', sqlerrm);
        end;
    end loop;

    -- What this call wrote, read back from the store rather than counted along the way (a
    -- rolled-back component leaves nothing to count).
    return jsonb_build_object(
        'minted', coalesce((
            select jsonb_object_agg(kind, written) from (
                select kind, count(*) as written from staging.entities
                where minted_by = v_version and minted_at >= v_started group by kind) minted), '{}'::jsonb),
        'aliases', coalesce((
            select jsonb_object_agg(scheme, written) from (
                select scheme, count(*) as written from staging.entity_aliases
                where mapping_version = v_version and recorded_at >= v_started group by scheme) aliases),
            '{}'::jsonb),
        'relations', coalesce((
            select jsonb_object_agg(relation_type, written) from (
                select relation_type, count(*) as written from staging.entity_relations
                where mapping_version = v_version and recorded_at >= v_started group by relation_type) relations),
            '{}'::jsonb),
        'held_back', coalesce((
            select jsonb_object_agg(status, claims) from (
                select status, count(*) as claims from pg_temp.entity_backfill_claims
                where status <> 'planned' group by status) held), '{}'::jsonb),
        'failed', v_failed);
end;
$$;

comment on function staging.entity_backfill() is
    '#877 PR-2: apply staging.entity_backfill_plan. Idempotent; returns what it wrote, held back and failed. '
    'Run by the Dagster job entity_identity_backfill, never by a boot migration.';

-- Every boot: new legacy ids keep getting an entity until captures carry UUIDs (PR-3).

-- ---------------------------------------------------------------------------------------
-- When is a backfill due? (the sensor's reads)
-- ---------------------------------------------------------------------------------------

-- The newest ingestion time among the backfill's inputs. `recorded_at` is the audit clock,
-- and that is what this is: an operational cursor for "has anything arrived since the
-- last launch", never a knowable-at time for data.
create or replace function staging.entity_evidence_watermark()
returns timestamptz
language sql stable
set jit = off
as $$
    select greatest(
        (select max(recorded_at) from staging.capture_normalized_observations),
        (select max(recorded_at) from staging.contract_objects where contract_kind like 'universe-list:%'),
        (select max(recorded_at) from staging.fund_holding_facts),
        (select max(recorded_at) from staging.kg_identifiers),
        (select max(recorded_at) from staging.kg_edges));
$$;

-- Why a backfill is due, or null:
--   store-empty             the store holds nothing while ids are in use
--   unseen-legacy-id        an observed or published id names no entity yet (full check:
--                           a failed run is retried at the next evidence)
--   new-crosswalk-evidence  N-PORT or knowledge-graph rows arrived after `p_since`
--                           (null = ever), which may prove a new link
create or replace function staging.entity_backfill_due(p_since timestamptz)
returns text
language sql stable
set jit = off
as $$
    with used(legacy_id) as (
        select distinct used.legacy_id
        from staging.capture_observation_payloads payload
        cross join lateral (values
            (payload.normalized_payload->>'issuer_id'),
            (payload.normalized_payload->>'instrument_id'),
            (payload.normalized_payload->>'listing_id')) as used(legacy_id)
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
                select 1 from staging.kg_identifiers
                where recorded_at > coalesce(p_since, '-infinity'::timestamptz))
          or exists (
                select 1 from staging.kg_edges
                where recorded_at > coalesce(p_since, '-infinity'::timestamptz))
            then 'new-crosswalk-evidence'
    end;
$$;
