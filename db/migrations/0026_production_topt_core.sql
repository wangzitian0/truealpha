-- Exact Production TOPT snapshots and append-only core factor materialization.

create table if not exists raw.production_topt_run_plans (
    run_id                          text primary key references raw.capture_runs(run_id),
    release_manifest_id             text not null
        check (release_manifest_id ~ '^release-manifest:[0-9a-f]{64}$'),
    content_sha256                  text not null unique check (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload                         jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                      timestamptz not null default clock_timestamp()
);

create or replace function raw.validate_production_topt_run_plan()
returns trigger language plpgsql as $$
begin
    if raw.canonical_sha256(new.payload) <> new.content_sha256
       or new.payload->>'run_id' <> new.run_id
       or new.payload->>'release_manifest_id' <> new.release_manifest_id then
        raise check_violation using message = 'Production TOPT run plan identity drifted';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_plan on raw.production_topt_run_plans;
create trigger validate_plan
before insert on raw.production_topt_run_plans
for each row execute function raw.validate_production_topt_run_plan();

drop trigger if exists reject_mutation on raw.production_topt_run_plans;
create trigger reject_mutation
before update or delete on raw.production_topt_run_plans
for each row execute function raw.reject_capture_control_mutation();

create table if not exists staging.capture_observation_payloads (
    observation_id                 text primary key
        references staging.capture_normalized_observations(observation_id),
    normalized_payload_sha256      text not null check (normalized_payload_sha256 ~ '^[0-9a-f]{64}$'),
    normalized_payload             jsonb not null check (jsonb_typeof(normalized_payload) = 'object'),
    created_at                     timestamptz not null default clock_timestamp()
);

-- One immutable semantic observation may satisfy later unchanged obligations.
-- Keep the original capture_obligation_id as its creation coordinate and add
-- an append-only many-to-many usage binding for replay/idempotency.
create table if not exists staging.capture_observation_obligations (
    capture_obligation_id          text not null references raw.capture_obligations(obligation_id),
    observation_id                 text not null references staging.capture_normalized_observations(observation_id),
    bound_at                       timestamptz not null default clock_timestamp(),
    primary key (capture_obligation_id, observation_id)
);

insert into staging.capture_observation_obligations (capture_obligation_id, observation_id, bound_at)
select capture_obligation_id, observation_id, recorded_at
from staging.capture_normalized_observations
on conflict do nothing;

drop trigger if exists reject_mutation on staging.capture_observation_obligations;
create trigger reject_mutation
before update or delete on staging.capture_observation_obligations
for each row execute function raw.reject_capture_control_mutation();

create or replace function staging.validate_capture_observation_payload()
returns trigger language plpgsql as $$
declare
    expected_sha256 text;
begin
    select observation.normalized_payload_sha256
      into expected_sha256
      from staging.capture_normalized_observations observation
     where observation.observation_id = new.observation_id;
    if expected_sha256 is null
       or expected_sha256 <> new.normalized_payload_sha256
       or expected_sha256 <> raw.canonical_sha256(new.normalized_payload) then
        raise check_violation using message = 'capture observation payload does not match its normalized hash';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_payload on staging.capture_observation_payloads;
create trigger validate_payload
before insert on staging.capture_observation_payloads
for each row execute function staging.validate_capture_observation_payload();

drop trigger if exists reject_mutation on staging.capture_observation_payloads;
create trigger reject_mutation
before update or delete on staging.capture_observation_payloads
for each row execute function raw.reject_capture_control_mutation();

create table if not exists staging.topt_core_snapshots (
    snapshot_id                     text primary key
        check (snapshot_id ~ '^topt-core-snapshot:[0-9a-f]{64}$'),
    content_sha256                  text not null unique check (content_sha256 ~ '^[0-9a-f]{64}$'),
    run_id                          text not null unique references raw.capture_runs(run_id),
    release_manifest_id             text not null,
    universe_id                     text not null,
    universe_version                text not null,
    universe_sha256                 text not null check (universe_sha256 ~ '^[0-9a-f]{64}$'),
    cutoff                          timestamptz not null,
    issuer_count                    integer not null check (issuer_count = 20),
    instrument_count                integer not null check (instrument_count = 21),
    observation_count               integer not null check (observation_count = 84),
    payload                         jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                      timestamptz not null default clock_timestamp(),
    check (split_part(snapshot_id, ':', 2) = content_sha256)
);

create table if not exists staging.topt_core_snapshot_members (
    snapshot_id                     text not null references staging.topt_core_snapshots(snapshot_id),
    instrument_id                   text not null,
    issuer_id                       text not null,
    listing_id                      text not null,
    observation_ids                 text[] not null check (cardinality(observation_ids) = 4),
    member_sha256                   text not null check (member_sha256 ~ '^[0-9a-f]{64}$'),
    factor_input                    jsonb not null check (jsonb_typeof(factor_input) = 'object'),
    created_at                      timestamptz not null default clock_timestamp(),
    primary key (snapshot_id, instrument_id),
    unique (snapshot_id, listing_id)
);

create or replace function staging.validate_topt_core_snapshot()
returns trigger language plpgsql as $$
declare
    capture_status mart.topt_capture_status%rowtype;
    release_exists boolean;
    release_matches_plan boolean;
begin
    select * into capture_status from mart.topt_capture_status where run_id = new.run_id;
    if capture_status.run_id is null
       or capture_status.environment <> 'production'
       or capture_status.obligation_count <> 84
       or capture_status.terminal_count <> 84
       or capture_status.success_count + capture_status.unchanged_count <> 84
       or capture_status.unavailable_count <> 0
       or capture_status.skipped_count <> 0
       or capture_status.failed_count <> 0
       or not capture_status.complete
       or capture_status.universe_id <> new.universe_id
       or capture_status.universe_version <> new.universe_version
       or capture_status.universe_sha256 <> new.universe_sha256
       or capture_status.cutoff <> new.cutoff then
        raise check_violation using message = 'TOPT core snapshot requires one complete exact Production capture run';
    end if;
    select exists (
        select 1 from staging.contract_objects
         where contract_id = new.release_manifest_id and contract_kind = 'release_manifest'
    ) into release_exists;
    select exists (
        select 1 from raw.production_topt_run_plans
         where run_id = new.run_id and release_manifest_id = new.release_manifest_id
    ) into release_matches_plan;
    if not release_exists or not release_matches_plan then
        raise check_violation using message = 'TOPT core snapshot release is not durable or does not match its run plan';
    end if;
    if raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'TOPT core snapshot payload hash does not match';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_snapshot on staging.topt_core_snapshots;
create trigger validate_snapshot
before insert on staging.topt_core_snapshots
for each row execute function staging.validate_topt_core_snapshot();

create or replace function staging.validate_topt_core_snapshot_member()
returns trigger language plpgsql as $$
begin
    if new.observation_ids <> array(select unnest(new.observation_ids) order by 1)
       or cardinality(new.observation_ids) <> cardinality(array(select distinct unnest(new.observation_ids)))
       or raw.canonical_sha256(new.factor_input) <> new.member_sha256
       or new.factor_input->>'snapshot_id' <> new.snapshot_id
       or new.factor_input->>'instrument_id' <> new.instrument_id
       or new.factor_input->>'issuer_id' <> new.issuer_id
       or new.factor_input->>'listing_id' <> new.listing_id then
        raise check_violation using message = 'TOPT core snapshot member identity or payload drifted';
    end if;
    if exists (
        select 1 from unnest(new.observation_ids) observation_id
        left join staging.capture_normalized_observations observation using (observation_id)
        left join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id is null or payload.observation_id is null
    ) then
        raise check_violation using message = 'TOPT core snapshot member lacks durable normalized payload lineage';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_member on staging.topt_core_snapshot_members;
create trigger validate_member
before insert on staging.topt_core_snapshot_members
for each row execute function staging.validate_topt_core_snapshot_member();

do $$
declare
    target regclass;
begin
    foreach target in array array[
        'staging.topt_core_snapshots'::regclass,
        'staging.topt_core_snapshot_members'::regclass
    ] loop
        execute format('drop trigger if exists reject_mutation on %s', target);
        execute format(
            'create trigger reject_mutation before update or delete on %s '
            'for each row execute function raw.reject_capture_control_mutation()',
            target
        );
    end loop;
end $$;

create table if not exists mart.topt_core_invocations (
    invocation_id                   text primary key
        check (invocation_id ~ '^topt-core-invocation:[0-9a-f]{64}$'),
    content_sha256                  text not null unique check (content_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_id                     text not null references staging.topt_core_snapshots(snapshot_id),
    gppe_definition_id              text not null check (gppe_definition_id ~ '^gppe-definition:[0-9a-f]{64}$'),
    gppe_definition_sha256          text not null check (gppe_definition_sha256 ~ '^[0-9a-f]{64}$'),
    tier_definition_id              text not null check (tier_definition_id ~ '^three-tier-definition:[0-9a-f]{64}$'),
    tier_definition_sha256          text not null check (tier_definition_sha256 ~ '^[0-9a-f]{64}$'),
    payload                         jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                      timestamptz not null default clock_timestamp(),
    unique (snapshot_id, gppe_definition_id, tier_definition_id),
    check (split_part(invocation_id, ':', 2) = content_sha256)
);

create table if not exists mart.topt_core_results (
    result_id                       text primary key
        check (result_id ~ '^topt-core-result:[0-9a-f]{64}$'),
    content_sha256                  text not null unique check (content_sha256 ~ '^[0-9a-f]{64}$'),
    invocation_id                   text not null references mart.topt_core_invocations(invocation_id),
    snapshot_id                     text not null references staging.topt_core_snapshots(snapshot_id),
    run_id                          text not null references raw.capture_runs(run_id),
    release_manifest_id             text not null,
    universe_id                     text not null,
    universe_version                text not null,
    universe_sha256                 text not null check (universe_sha256 ~ '^[0-9a-f]{64}$'),
    cutoff                          timestamptz not null,
    issuer_id                       text not null,
    instrument_id                   text not null,
    listing_id                      text not null,
    availability                    text not null check (availability in ('available', 'unavailable')),
    capital_adjusted_gross_profit   numeric,
    gppe                            numeric,
    tier                            text check (tier in ('traditional', 'tech', 'large_model_native')),
    target_ps_lower                 numeric,
    target_ps_upper                 numeric,
    target_ps_midpoint              numeric,
    current_ps                      numeric,
    valuation_gap                   numeric,
    confidence                      numeric not null check (confidence between 0 and 1),
    freshness                       text not null check (freshness in ('fresh', 'stale', 'unknown')),
    reason_codes                    text[] not null,
    input_observation_ids           text[] not null check (cardinality(input_observation_ids) = 4),
    gppe_definition_id              text not null,
    gppe_definition_sha256          text not null check (gppe_definition_sha256 ~ '^[0-9a-f]{64}$'),
    tier_definition_id              text not null,
    tier_definition_sha256          text not null check (tier_definition_sha256 ~ '^[0-9a-f]{64}$'),
    payload                         jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                      timestamptz not null default clock_timestamp(),
    unique (invocation_id, instrument_id),
    check (split_part(result_id, ':', 2) = content_sha256),
    check (
        (availability = 'available' and capital_adjusted_gross_profit is not null and gppe is not null
            and tier is not null and target_ps_lower is not null and target_ps_upper is not null
            and target_ps_midpoint is not null and current_ps is not null and valuation_gap is not null
            and cardinality(reason_codes) = 0)
        or
        (availability = 'unavailable' and capital_adjusted_gross_profit is null and gppe is null
            and tier is null and target_ps_lower is null and target_ps_upper is null
            and target_ps_midpoint is null and current_ps is null and valuation_gap is null
            and cardinality(reason_codes) > 0)
    )
);

create or replace function mart.validate_topt_core_result()
returns trigger language plpgsql as $$
declare
    invocation mart.topt_core_invocations%rowtype;
begin
    select * into invocation from mart.topt_core_invocations where invocation_id = new.invocation_id;
    if invocation.invocation_id is null
       or invocation.snapshot_id <> new.snapshot_id
       or invocation.gppe_definition_id <> new.gppe_definition_id
       or invocation.gppe_definition_sha256 <> new.gppe_definition_sha256
       or invocation.tier_definition_id <> new.tier_definition_id
       or invocation.tier_definition_sha256 <> new.tier_definition_sha256
       or raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'TOPT core result does not match its invocation or content';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_result on mart.topt_core_results;
create trigger validate_result
before insert on mart.topt_core_results
for each row execute function mart.validate_topt_core_result();

do $$
declare
    target regclass;
begin
    foreach target in array array[
        'mart.topt_core_invocations'::regclass,
        'mart.topt_core_results'::regclass
    ] loop
        execute format('drop trigger if exists reject_mutation on %s', target);
        execute format(
            'create trigger reject_mutation before update or delete on %s '
            'for each row execute function raw.reject_capture_control_mutation()',
            target
        );
    end loop;
end $$;

create or replace view mart.topt_core_result_read as
select
    result.result_id,
    result.invocation_id,
    result.snapshot_id,
    result.run_id,
    result.release_manifest_id,
    result.universe_id,
    result.universe_version,
    result.universe_sha256,
    result.cutoff,
    result.issuer_id,
    result.instrument_id,
    result.listing_id,
    result.availability,
    result.capital_adjusted_gross_profit,
    result.gppe,
    result.tier,
    result.target_ps_lower,
    result.target_ps_upper,
    result.target_ps_midpoint,
    result.current_ps,
    result.valuation_gap,
    result.confidence,
    result.freshness,
    result.reason_codes,
    result.gppe_definition_id,
    result.gppe_definition_sha256,
    result.tier_definition_id,
    result.tier_definition_sha256,
    result.created_at
from mart.topt_core_results result;

create or replace view mart.topt_core_meta_info as
select
    result.result_id,
    result.invocation_id,
    result.snapshot_id,
    result.run_id,
    result.release_manifest_id,
    result.universe_id,
    result.universe_version,
    result.universe_sha256,
    result.cutoff,
    result.issuer_id,
    result.instrument_id,
    result.listing_id,
    result.input_observation_ids,
    result.gppe_definition_id,
    result.gppe_definition_sha256,
    result.tier_definition_id,
    result.tier_definition_sha256,
    result.confidence,
    result.freshness,
    result.created_at,
    lineage.items as lineage
from mart.topt_core_results result
join lateral (
    select jsonb_agg(
        jsonb_build_object(
            'observation_id', observation.observation_id,
            'semantic_type', observation.semantic_type,
            'semantic_version', observation.semantic_version,
            'source_vintage_id', observation.source_vintage_id,
            'source_request_id', vintage.source_request_id,
            'source_registry_entry_id', request.source_registry_entry_id,
            'source_policy_id', request.source_policy_id,
            'parser_version', observation.parser_version,
            'mapping_version', observation.mapping_version,
            'normalized_payload_sha256', observation.normalized_payload_sha256,
            'confidence', observation.confidence,
            'freshness', observation.freshness_state,
            'knowable_at', observation.knowable_at,
            'recorded_at', observation.recorded_at
        ) order by observation.observation_id
    ) as items
    from unnest(result.input_observation_ids) selected(observation_id)
    join staging.capture_normalized_observations observation using (observation_id)
    join raw.capture_source_vintages vintage using (source_vintage_id)
    join raw.capture_source_requests request using (source_request_id)
) lineage on true;

create or replace view mart.topt_capture_meta_info as
select
    obligation.run_id,
    obligation.obligation_id,
    result.logical_obligation_id,
    obligation.subject_kind,
    obligation.subject_id,
    obligation.capture_requirement_id,
    obligation.partition_key,
    binding.work_item_id,
    work.source_request_id,
    request.source_registry_entry_id,
    request.source_policy_id,
    request.request_fingerprint_version,
    result.terminal_state,
    result.reason_codes,
    result.completed_at,
    coalesce(attempts.attempt_count, 0)::integer as attempt_count,
    final_attempt_result.status_code as final_status_code,
    observation.observation_id,
    observation.semantic_version,
    observation.parser_version,
    observation.mapping_version,
    observation.confidence,
    observation.freshness_state,
    observation.knowable_at,
    observation.recorded_at
from raw.capture_obligations obligation
left join raw.capture_obligation_work_bindings binding
    on binding.obligation_id = obligation.obligation_id
left join raw.capture_work_items work using (work_item_id)
left join raw.capture_source_requests request using (source_request_id)
left join raw.capture_obligation_results result
    on result.capture_obligation_id = obligation.obligation_id
left join raw.capture_attempt_results final_attempt_result
    on final_attempt_result.attempt_id = result.final_attempt_id
left join lateral (
    select count(*) as attempt_count
    from raw.capture_attempts attempt
    where attempt.work_item_id = work.work_item_id
) attempts on true
left join lateral (
    select candidate.*
    from staging.capture_observation_obligations usage
    join staging.capture_normalized_observations candidate using (observation_id)
    where usage.capture_obligation_id = obligation.obligation_id
    order by candidate.recorded_at desc, candidate.observation_id desc
    limit 1
) observation on true;

grant select on mart.topt_core_result_read, mart.topt_core_meta_info to mart_readonly;
