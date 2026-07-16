-- Governed multi-user research identity and private-state boundary.
-- Authorization remains a server-side decision; these tables never feed factors.

create schema if not exists app;

create or replace function app.reject_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'app identity, policy, and audit records are append-only; insert a new event';
end;
$$;

create table if not exists app.tenants (
    tenant_id      text primary key check (length(tenant_id) > 0),
    recorded_at    timestamptz not null default now()
);

create table if not exists app.principals (
    principal_id   text primary key check (length(principal_id) > 0),
    tenant_id      text not null references app.tenants (tenant_id),
    principal_kind text not null check (principal_kind in ('member', 'administrator', 'service')),
    recorded_at    timestamptz not null default now()
);

create table if not exists app.tenant_memberships (
    membership_event_id text primary key check (length(membership_event_id) > 0),
    tenant_id            text not null references app.tenants (tenant_id),
    principal_id         text not null references app.principals (principal_id),
    membership_state     text not null check (membership_state in ('granted', 'revoked')),
    effective_at         timestamptz not null,
    recorded_at          timestamptz not null default now(),
    check (recorded_at >= effective_at)
);

create table if not exists app.entitlement_grants (
    grant_id              text primary key check (length(grant_id) > 0),
    tenant_id             text not null references app.tenants (tenant_id),
    principal_id          text not null references app.principals (principal_id),
    entitlement_id        text not null check (length(entitlement_id) > 0),
    publication_policy_id text not null check (length(publication_policy_id) > 0),
    valid_from            timestamptz not null,
    valid_until           timestamptz not null,
    recorded_at           timestamptz not null default now(),
    check (valid_until > valid_from),
    check (recorded_at >= valid_from)
);

create table if not exists app.grant_revocations (
    revocation_id text primary key check (length(revocation_id) > 0),
    tenant_id     text not null references app.tenants (tenant_id),
    grant_id      text not null references app.entitlement_grants (grant_id),
    revoked_at    timestamptz not null,
    reason_code   text not null check (length(reason_code) > 0),
    recorded_at   timestamptz not null default now(),
    check (recorded_at >= revoked_at)
);

create table if not exists app.publication_policies (
    publication_policy_event_id text primary key check (length(publication_policy_event_id) > 0),
    publication_policy_id       text not null check (length(publication_policy_id) > 0),
    publication_class_id        text not null check (length(publication_class_id) > 0),
    permitted                   boolean not null,
    successor_policy_id         text,
    effective_at                timestamptz not null,
    recorded_at                 timestamptz not null default now(),
    check (recorded_at >= effective_at),
    check (successor_policy_id is null or successor_policy_id <> publication_policy_id)
);

create table if not exists app.private_research_objects (
    resource_id       text primary key check (length(resource_id) > 0),
    tenant_id         text not null references app.tenants (tenant_id),
    owner_principal_id text not null references app.principals (principal_id),
    resource_type     text not null check (resource_type in ('private_conversation', 'private_document')),
    object_ref        text not null check (length(object_ref) > 0),
    recorded_at       timestamptz not null default now()
);

create index if not exists idx_private_research_objects_owner
    on app.private_research_objects (tenant_id, owner_principal_id, resource_id);

alter table app.private_research_objects enable row level security;
alter table app.private_research_objects force row level security;

drop policy if exists private_research_owner_isolation on app.private_research_objects;
create policy private_research_owner_isolation on app.private_research_objects
    for select
    using (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    );

create table if not exists app.authorization_decisions (
    decision_id           text primary key check (decision_id ~ '^access-decision:[0-9a-f]{64}$'),
    tenant_id             text,
    principal_id          text,
    action                text not null check (length(action) > 0),
    resource_id           text not null check (length(resource_id) > 0),
    publication_policy_id text not null check (length(publication_policy_id) > 0),
    decision              text not null check (decision in ('allow', 'deny')),
    reason_code           text,
    decided_at            timestamptz not null,
    recorded_at           timestamptz not null default now(),
    check ((decision = 'allow' and reason_code is null) or (decision = 'deny' and reason_code is not null)),
    check (recorded_at >= decided_at)
);

create table if not exists app.access_audit_events (
    audit_event_id text primary key check (length(audit_event_id) > 0),
    decision_id    text not null references app.authorization_decisions (decision_id),
    tenant_id      text,
    principal_id   text,
    event_kind     text not null check (event_kind in ('access_allowed', 'access_denied', 'authentication_denied')),
    occurred_at    timestamptz not null,
    recorded_at    timestamptz not null default now(),
    check (recorded_at >= occurred_at)
);

create or replace function app.validate_access_audit_decision_tenant()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, app
as $$
declare
    decision_tenant_id text;
begin
    select tenant_id into decision_tenant_id
    from app.authorization_decisions
    where decision_id = new.decision_id;

    if not found then
        raise exception 'access audit decision does not exist';
    end if;
    if decision_tenant_id is distinct from new.tenant_id then
        raise exception 'access audit tenant must match its authorization decision';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_access_audit_events_validate_tenant on app.access_audit_events;
create trigger trg_access_audit_events_validate_tenant
before insert on app.access_audit_events
for each row execute function app.validate_access_audit_decision_tenant();

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
    event.recorded_at
from app.access_audit_events as event
join app.authorization_decisions as decision
  on decision.decision_id = event.decision_id
 and decision.tenant_id is not distinct from event.tenant_id
where event.tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
  and exists (
      select 1
      from app.principals as reader
      where reader.principal_id = nullif(current_setting('truealpha.principal_id', true), '')
        and reader.principal_kind = 'administrator'
  );

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'tenants',
        'principals',
        'tenant_memberships',
        'entitlement_grants',
        'grant_revocations',
        'publication_policies',
        'private_research_objects',
        'authorization_decisions',
        'access_audit_events'
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
