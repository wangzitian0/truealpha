-- Wave 1 (#1747): converge governed_strategy_run onto mart.served_head,
-- persist output invariant facts, and store append-only head rollback requests.

do $$ begin
  if position('served_head' in pg_get_viewdef('mart.governed_strategy_run'::regclass, true)) = 0 then
    execute $vc$create or replace view mart.governed_strategy_run as
    with head as (
        select distinct on (regexp_replace(served_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''))
            served_head.environment,
            served_head.universe_id,
            served_head.universe_version,
            served_head.factor_id,
            served_head.run_id as target_run_id,
            served_head.sequence,
            served_head.advanced_at
        from mart.served_head
        where served_head.environment = ((
            select environment_identity.environment
            from mart.environment_identity
        ))
          and served_head.factor_id = 'gross_profit_per_employee'::text
          and served_head.universe_id not like 'universe:canary-%'::text
          and served_head.run_id is not null
        order by regexp_replace(served_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''),
                 served_head.advanced_at desc
    )
    select head.environment,
        head.universe_id,
        head.universe_version,
        head.factor_id,
        head.target_run_id,
        head.sequence,
        head.advanced_at,
        campaign.cutoff,
        run.strategy_run_id,
        run.strategy_key,
        run.executed_at
    from head
    join raw.capture_runs cr on cr.run_id = head.target_run_id
    join raw.capture_campaigns campaign using (campaign_id)
    join lateral (
        select distinct on (candidate.strategy_key)
            candidate.strategy_run_id,
            candidate.strategy_key,
            candidate.executed_at
        from mart.strategy_run_capture_bindings binding
        join mart.strategy_runs candidate on candidate.strategy_run_id = binding.strategy_run_id
        where binding.capture_run_id = head.target_run_id
        order by candidate.strategy_key, binding.bound_at desc, candidate.strategy_run_id desc
    ) run on true$vc$;
  end if;
end $$;

create table if not exists mart.output_invariant_results (
    result_id bigint generated always as identity,
    dagster_run_id text not null,
    checked_at timestamp with time zone not null,
    invariant_id text not null,
    kind text not null,
    population integer not null,
    violation_count integer not null,
    deferred boolean default false not null,
    sample_rows jsonb default '[]'::jsonb not null,
    recorded_at timestamp with time zone default clock_timestamp() not null,
    constraint output_invariant_results_pkey primary key (result_id),
    constraint output_invariant_results_kind_check check (kind in ('hard', 'fact')),
    constraint output_invariant_results_population_check check (population >= 0),
    constraint output_invariant_results_violation_count_check check (violation_count >= 0),
    constraint output_invariant_results_dagster_run_id_check check (char_length(dagster_run_id) between 1 and 64),
    constraint output_invariant_results_invariant_id_check check (invariant_id ~ '^[a-z0-9-]+$')
);

do $$ begin
  if not exists (
    select 1 from pg_trigger
    where tgrelid = 'mart.output_invariant_results'::regclass
      and not tgisinternal
      and tgname = 'reject_mutation'
  ) then
    create trigger reject_mutation
      before delete or update on mart.output_invariant_results
      for each row execute function mart.reject_mutation();
  end if;
end $$;

do $$ begin
  if to_regclass('mart.ix_output_invariant_results_latest') is null then
    create index ix_output_invariant_results_latest
      on mart.output_invariant_results using btree (checked_at desc, invariant_id);
  end if;
end $$;

create table if not exists staging.head_rollback_requests (
    request_id bigint generated always as identity,
    environment text not null,
    universe_id text not null,
    universe_version text not null,
    factor_id text not null,
    target_run_id text not null,
    reason text not null,
    requested_by text not null,
    dedupe_key text not null,
    requested_at timestamp with time zone default clock_timestamp() not null,
    consumed_at timestamp with time zone,
    result_pointer_id text,
    constraint head_rollback_requests_pkey primary key (request_id),
    constraint head_rollback_requests_dedupe_key_key unique (dedupe_key),
    constraint head_rollback_requests_consumed_pair_check check ((consumed_at is null) = (result_pointer_id is null)),
    constraint head_rollback_requests_reason_check check (length(btrim(reason)) > 0),
    constraint head_rollback_requests_requested_by_check check (length(btrim(requested_by)) > 0),
    constraint head_rollback_requests_target_run_check check (split_part(target_run_id, ':', 1) = 'capture-run')
);

create or replace function staging.reject_head_rollback_delete()
 returns trigger
 language plpgsql
as $function$
begin
    raise check_violation using message = 'head rollback requests are an append-only audit trail';
end;
$function$;

create or replace function staging.validate_head_rollback_update()
 returns trigger
 language plpgsql
as $function$
begin
    if old.consumed_at is not null then
        raise check_violation using message = 'head rollback request already consumed';
    end if;
    if new.request_id <> old.request_id
       or new.environment <> old.environment
       or new.universe_id <> old.universe_id
       or new.universe_version <> old.universe_version
       or new.factor_id <> old.factor_id
       or new.target_run_id <> old.target_run_id
       or new.reason <> old.reason
       or new.requested_by <> old.requested_by
       or new.dedupe_key <> old.dedupe_key
       or new.requested_at <> old.requested_at then
        raise check_violation using message = 'head rollback request fields are immutable';
    end if;
    return new;
end;
$function$;

do $$ begin
  if not exists (
    select 1 from pg_trigger
    where tgrelid = 'staging.head_rollback_requests'::regclass
      and not tgisinternal
      and tgname = 'reject_head_rollback_delete'
  ) then
    create trigger reject_head_rollback_delete
      before delete on staging.head_rollback_requests
      for each row execute function staging.reject_head_rollback_delete();
  end if;
end $$;

do $$ begin
  if not exists (
    select 1 from pg_trigger
    where tgrelid = 'staging.head_rollback_requests'::regclass
      and not tgisinternal
      and tgname = 'validate_head_rollback_update'
  ) then
    create trigger validate_head_rollback_update
      before update on staging.head_rollback_requests
      for each row execute function staging.validate_head_rollback_update();
  end if;
end $$;
