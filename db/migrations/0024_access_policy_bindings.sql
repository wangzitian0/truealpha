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
    if not exists (
        select 1
        from app.publication_policy_sets as policy_set
        where policy_set.publication_policy_set_id = new.publication_policy_id
    ) then
        raise exception 'authorization decision policy set does not exist';
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
    if decision_record.tenant_id is distinct from grant_record.tenant_id
       or decision_record.principal_id is distinct from grant_record.principal_id then
        raise exception 'authorization decision grant identity mismatch';
    end if;
    if decision_record.publication_policy_id <> grant_record.publication_policy_id then
        raise exception 'authorization decision grant policy mismatch';
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

create or replace view app.access_audit_metadata
with (security_barrier = true)
as
select
    event.audit_event_id,
    event.decision_id,
    event.tenant_id,
    event.principal_id,
    decision.action,
    decision.resource_id,
    decision.publication_policy_id,
    decision.decision,
    decision.reason_code,
    event.event_kind,
    event.occurred_at,
    event.recorded_at,
    policy_set.content_sha256 as publication_policy_content_sha256,
    policy_set.release_manifest_id,
    coalesce(
        array_agg(decision_grant.grant_id order by decision_grant.grant_id)
            filter (where decision_grant.grant_id is not null),
        array[]::text[]
    ) as entitlement_grant_ids
from app.access_audit_events as event
join app.authorization_decisions as decision
  on decision.decision_id = event.decision_id
 and decision.tenant_id is not distinct from event.tenant_id
join app.publication_policy_sets as policy_set
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
group by event.audit_event_id, decision.decision_id, policy_set.publication_policy_set_id;

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'publication_policy_sets',
        'publication_policy_entitlements',
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
