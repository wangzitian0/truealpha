-- Governed multi-user research identity and private-state boundary.
-- Authorization remains a server-side decision; these tables never feed factors.
--
-- #1061: app.tenant_memberships, app.publication_policies and app.private_research_objects
-- are retired. 20261006T1325_datahub_retire_empty_planes.sql drops them in deployed databases.
--
-- #1240: app.grant_revocations, app.authorization_decisions, app.access_audit_events,
-- app.access_audit_metadata and their trigger functions are retired.
-- 20261012T1130_runtime_retire_app_access_tables.sql drops them in deployed databases.
-- 0024_access_policy_bindings.sql no longer creates the other four tables of the access plane.

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

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'tenants',
        'principals',
        'entitlement_grants'
    ]
    loop
        -- Boot-lock guard: drop + create is SHARE ROW EXCLUSIVE; skip it when the trigger
        -- already matches the canonical (`pg_get_triggerdef`) form of this statement.
        if not exists (
            select 1
            from pg_trigger
            where tgrelid = format('app.%I', table_name)::regclass
              and not tgisinternal
              and pg_get_triggerdef(oid) = format(
                  'CREATE TRIGGER %I BEFORE DELETE OR UPDATE ON app.%I '
                  'FOR EACH ROW EXECUTE FUNCTION app.reject_mutation()',
                  'trg_' || table_name || '_append_only',
                  table_name
              )
        ) then
            execute format('drop trigger if exists %I on app.%I', 'trg_' || table_name || '_append_only', table_name);
            execute format(
                'create trigger %I before update or delete on app.%I for each row execute function app.reject_mutation()',
                'trg_' || table_name || '_append_only',
                table_name
            );
        end if;
    end loop;
end;
$$;
