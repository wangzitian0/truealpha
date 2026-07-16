-- Stable E2 bindings between immutable release policy, runtime grants, and decisions.

create table if not exists app.publication_policy_sets (
    publication_policy_set_id text primary key check (length(publication_policy_set_id) > 0),
    content_sha256            text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    release_manifest_id       text not null check (length(release_manifest_id) > 0),
    recorded_at               timestamptz not null default now()
);

create table if not exists app.publication_policy_entitlements (
    publication_policy_rule_id text primary key check (length(publication_policy_rule_id) > 0),
    publication_policy_set_id  text not null references app.publication_policy_sets,
    publication_class_id       text not null check (length(publication_class_id) > 0),
    entitlement_id             text not null check (length(entitlement_id) > 0),
    recorded_at                timestamptz not null default now(),
    unique (publication_policy_set_id, publication_class_id, entitlement_id)
);

create table if not exists app.publication_policy_set_seals (
    publication_policy_set_id text primary key references app.publication_policy_sets,
    content_sha256            text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    sealed_at                 timestamptz not null,
    recorded_at               timestamptz not null default now(),
    check (recorded_at >= sealed_at)
);

create or replace function app.validate_publication_policy_entitlement_insert()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
begin
    perform 1
    from app.publication_policy_sets
    where publication_policy_set_id = new.publication_policy_set_id
    for update;

    if exists (
        select 1
        from app.publication_policy_set_seals as seal
        where seal.publication_policy_set_id = new.publication_policy_set_id
    ) then
        raise exception 'sealed publication policy set cannot accept new rules';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_publication_policy_entitlements_validate_insert
on app.publication_policy_entitlements;
create trigger trg_publication_policy_entitlements_validate_insert
before insert on app.publication_policy_entitlements
for each row execute function app.validate_publication_policy_entitlement_insert();

create or replace function app.validate_publication_policy_set_seal()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
declare
    policy_content_sha256 text;
begin
    select content_sha256 into policy_content_sha256
    from app.publication_policy_sets
    where publication_policy_set_id = new.publication_policy_set_id
    for update;

    if policy_content_sha256 is null then
        raise exception 'publication policy set does not exist';
    end if;
    if policy_content_sha256 <> new.content_sha256 then
        raise exception 'publication policy set seal content hash mismatch';
    end if;
    if not exists (
        select 1
        from app.publication_policy_entitlements as policy_rule
        where policy_rule.publication_policy_set_id = new.publication_policy_set_id
    ) then
        raise exception 'publication policy set cannot be sealed without rules';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_publication_policy_set_seals_validate
on app.publication_policy_set_seals;
create trigger trg_publication_policy_set_seals_validate
before insert on app.publication_policy_set_seals
for each row execute function app.validate_publication_policy_set_seal();

alter table app.authorization_decisions
    add column if not exists resource_type text,
    add column if not exists publication_class_id text;

create table if not exists app.authorization_decision_grants (
    decision_id text not null references app.authorization_decisions,
    grant_id    text not null references app.entitlement_grants,
    recorded_at timestamptz not null default now(),
    primary key (decision_id, grant_id)
);

create or replace function app.validate_authorization_decision_policy_set()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
begin
    if new.resource_type is null then
        if new.publication_class_id is not null then
            raise exception 'legacy authorization decision cannot carry a publication class';
        end if;
        return new;
    end if;
    if new.resource_type not in (
        'private_conversation',
        'private_document',
        'materialized_strategy_result',
        'materialized_backtest_result',
        'access_audit_metadata',
        'registered_replay_definition'
    ) then
        raise exception 'authorization decision resource type is invalid';
    end if;
    if (new.resource_type in ('materialized_strategy_result', 'materialized_backtest_result'))
       is distinct from (new.publication_class_id is not null) then
        raise exception 'authorization decision publication class shape is invalid';
    end if;
    if not exists (
        select 1
        from app.publication_policy_sets as policy_set
        join app.publication_policy_set_seals as seal
          on seal.publication_policy_set_id = policy_set.publication_policy_set_id
         and seal.content_sha256 = policy_set.content_sha256
        where policy_set.publication_policy_set_id = new.publication_policy_id
    ) then
        raise exception 'authorization decision policy set is missing or unsealed';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_authorization_decisions_validate_policy_set on app.authorization_decisions;
create trigger trg_authorization_decisions_validate_policy_set
before insert on app.authorization_decisions
for each row execute function app.validate_authorization_decision_policy_set();

create or replace function app.validate_authorization_decision_grant()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
declare
    decision_record app.authorization_decisions%rowtype;
    grant_record app.entitlement_grants%rowtype;
begin
    select * into decision_record
    from app.authorization_decisions
    where decision_id = new.decision_id;

    select * into grant_record
    from app.entitlement_grants
    where grant_id = new.grant_id;

    if decision_record.decision_id is null or grant_record.grant_id is null then
        raise exception 'authorization decision or entitlement grant does not exist';
    end if;
    if decision_record.decision <> 'allow' then
        raise exception 'denied authorization decision cannot claim an entitlement grant';
    end if;
    if decision_record.action <> 'read_materialized_result'
       or decision_record.resource_type not in (
           'materialized_strategy_result',
           'materialized_backtest_result'
       ) then
        raise exception 'only materialized-result decisions can claim an entitlement grant';
    end if;
    if decision_record.tenant_id is distinct from grant_record.tenant_id
       or decision_record.principal_id is distinct from grant_record.principal_id then
        raise exception 'authorization decision grant identity mismatch';
    end if;
    if decision_record.publication_policy_id <> grant_record.publication_policy_id then
        raise exception 'authorization decision grant policy mismatch';
    end if;
    if not exists (
        select 1
        from app.publication_policy_entitlements as policy_rule
        where policy_rule.publication_policy_set_id = decision_record.publication_policy_id
          and policy_rule.publication_class_id = decision_record.publication_class_id
          and policy_rule.entitlement_id = grant_record.entitlement_id
    ) then
        raise exception 'authorization decision grant entitlement is not permitted';
    end if;
    if decision_record.decided_at < grant_record.valid_from
       or (grant_record.valid_until is not null and decision_record.decided_at >= grant_record.valid_until)
       or exists (
           select 1
           from app.grant_revocations as revocation
           where revocation.grant_id = grant_record.grant_id
             and revocation.revoked_at <= decision_record.decided_at
       ) then
        raise exception 'authorization decision grant was not active at decision time';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_authorization_decision_grants_validate on app.authorization_decision_grants;
create trigger trg_authorization_decision_grants_validate
before insert on app.authorization_decision_grants
for each row execute function app.validate_authorization_decision_grant();

create or replace function app.validate_access_audit_decision_tenant()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
declare
    decision_record app.authorization_decisions%rowtype;
    expected_event_kind text;
begin
    select * into decision_record
    from app.authorization_decisions
    where decision_id = new.decision_id;

    if decision_record.decision_id is null then
        raise exception 'access audit decision does not exist';
    end if;
    if decision_record.tenant_id is distinct from new.tenant_id then
        raise exception 'access audit tenant must match its authorization decision';
    end if;
    if decision_record.principal_id is distinct from new.principal_id then
        raise exception 'access audit principal must match its authorization decision';
    end if;
    if decision_record.decided_at <> new.occurred_at then
        raise exception 'access audit time must match its authorization decision';
    end if;
    expected_event_kind := case
        when decision_record.decision = 'allow' then 'access_allowed'
        when decision_record.reason_code in (
            'authentication_missing',
            'authentication_invalid',
            'authentication_not_yet_valid',
            'authentication_expired',
            'delegation_revoked',
            'client_authority_claim_rejected'
        ) then 'authentication_denied'
        else 'access_denied'
    end;
    if new.event_kind <> expected_event_kind then
        raise exception 'access audit event kind must match its authorization decision';
    end if;
    return new;
end;
$$;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'app.access_audit_events'::regclass
          and conname = 'access_audit_events_content_id_check'
    ) then
        alter table app.access_audit_events
            add constraint access_audit_events_content_id_check
            check (audit_event_id ~ '^access-audit-event:[0-9a-f]{64}$') not valid;
    end if;
end;
$$;

create or replace view app.access_audit_metadata
with (security_barrier = true)
as
select
    event.audit_event_id,
    event.decision_id,
    event.tenant_id,
    event.principal_id,
    decision.action,
    case
        when decision.resource_type in ('private_conversation', 'private_document') then null
        else decision.resource_id
    end as resource_id,
    decision.publication_policy_id,
    decision.decision,
    decision.reason_code,
    event.event_kind,
    event.occurred_at,
    event.recorded_at,
    policy_set.content_sha256 as publication_policy_content_sha256,
    policy_set.release_manifest_id,
    decision.resource_type,
    decision.publication_class_id,
    coalesce(
        array_agg(decision_grant.grant_id order by decision_grant.grant_id)
            filter (where decision_grant.grant_id is not null),
        array[]::text[]
    ) as entitlement_grant_ids
from app.access_audit_events as event
join app.authorization_decisions as decision
  on decision.decision_id = event.decision_id
 and decision.tenant_id is not distinct from event.tenant_id
left join app.publication_policy_sets as policy_set
  on policy_set.publication_policy_set_id = decision.publication_policy_id
left join app.authorization_decision_grants as decision_grant
  on decision_grant.decision_id = decision.decision_id
where event.tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
  and exists (
      select 1
      from app.principals as reader
      where reader.principal_id = nullif(current_setting('truealpha.principal_id', true), '')
        and reader.principal_kind = 'administrator'
  )
group by
    event.audit_event_id,
    decision.decision_id,
    policy_set.publication_policy_set_id,
    policy_set.content_sha256,
    policy_set.release_manifest_id;

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'publication_policy_sets',
        'publication_policy_entitlements',
        'publication_policy_set_seals',
        'authorization_decision_grants'
    ]
    loop
        execute format('drop trigger if exists %I on app.%I', 'trg_' || table_name || '_append_only', table_name);
        execute format(
            'create trigger %I before update or delete on app.%I for each row execute function app.reject_mutation()',
            'trg_' || table_name || '_append_only',
            table_name
        );
    end loop;
end;
$$;
