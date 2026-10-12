-- Governed research access: the storage boundary that remains after #1240.
-- The decision, grant-binding, policy-set and audit tables are retired. No code wrote to them.
-- This contract checks two things:
--   * The retired relations and functions do not exist in a migrated database.
--   * The identity tables that stay (tenants, principals, entitlement grants) are append-only.
--   * The runtime and audit roles keep their grant boundary on the remaining audit views.
-- Run as: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/governed_research_access_contract.sql
-- ci-db runs this file on a freshly migrated database. The transaction rolls back.

begin;

do $$
declare
    retired_name text;
    append_only_trigger_count integer;
begin
    if to_regnamespace('app') is null then
        raise exception 'app schema is missing';
    end if;

    foreach retired_name in array array[
        'app.access_audit_events',
        'app.authorization_decisions',
        'app.authorization_decision_grants',
        'app.grant_revocations',
        'app.publication_policy_sets',
        'app.publication_policy_entitlements',
        'app.publication_policy_set_seals',
        'app.access_audit_metadata'
    ]
    loop
        if to_regclass(retired_name) is not null then
            raise exception 'retired access relation % exists', retired_name;
        end if;
    end loop;

    foreach retired_name in array array[
        'app.validate_access_audit_decision_tenant()',
        'app.validate_publication_policy_entitlement_insert()',
        'app.validate_publication_policy_set_seal()',
        'app.validate_authorization_decision_policy_set()',
        'app.validate_authorization_decision_grant()',
        'app.validate_authorization_decision_required_grants()'
    ]
    loop
        if to_regprocedure(retired_name) is not null then
            raise exception 'retired access function % exists', retired_name;
        end if;
    end loop;

    if to_regclass('app.tenants') is null
       or to_regclass('app.principals') is null
       or to_regclass('app.entitlement_grants') is null then
        raise exception 'governed identity storage boundary is incomplete';
    end if;

    select count(*) into append_only_trigger_count
    from pg_trigger
    where not tgisinternal
      and tgname like 'trg_%_append_only'
      and tgrelid in (
          'app.tenants'::regclass,
          'app.principals'::regclass,
          'app.entitlement_grants'::regclass
      );
    if append_only_trigger_count <> 3 then
        raise exception 'all governed identity records must be append-only';
    end if;

    if has_table_privilege('app_runtime', 'raw.fetches', 'select')
       or has_table_privilege('app_runtime', 'staging.financial_facts', 'select') then
        raise exception 'app runtime must not read raw or staging data';
    end if;
    if has_table_privilege('app_runtime', 'app.conversation_audit_metadata', 'select')
       or has_table_privilege('app_runtime', 'app.document_audit_metadata', 'select') then
        raise exception 'ordinary app runtime must not read administrator audit metadata';
    end if;
    if not has_table_privilege('app_audit_reader', 'app.conversation_audit_metadata', 'select')
       or not has_table_privilege('app_audit_reader', 'app.document_audit_metadata', 'select')
       or has_table_privilege('app_audit_reader', 'app.conversations', 'select')
       or has_table_privilege('app_audit_reader', 'app.research_documents', 'select') then
        raise exception 'audit reader must receive only the filtered metadata views';
    end if;
end;
$$;

insert into app.tenants (tenant_id, recorded_at)
values
    ('tenant:alpha', '2026-07-15T00:00:00Z'),
    ('tenant:platform', '2026-07-15T00:00:00Z');

insert into app.principals (principal_id, tenant_id, principal_kind, recorded_at)
values
    ('principal:alpha:alice', 'tenant:alpha', 'member', '2026-07-15T00:00:00Z'),
    ('principal:platform:admin', 'tenant:platform', 'administrator', '2026-07-15T00:00:00Z');

insert into app.entitlement_grants (
    grant_id,
    tenant_id,
    principal_id,
    entitlement_id,
    publication_policy_id,
    valid_from,
    valid_until,
    recorded_at
)
values (
    'grant:alpha:alice:001',
    'tenant:alpha',
    'principal:alpha:alice',
    'entitlement:research:standard:v1',
    'publication-policy-set:research:v2',
    '2026-07-15T00:00:00Z',
    '2026-07-15T01:00:00Z',
    '2026-07-15T00:00:00Z'
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
        begin
            execute format('update app.%I set recorded_at = recorded_at', table_name);
            raise exception 'append-only update unexpectedly succeeded for %', table_name;
        exception
            when raise_exception then
                if sqlerrm like 'append-only update unexpectedly succeeded%' then
                    raise;
                end if;
        end;
        begin
            execute format('delete from app.%I', table_name);
            raise exception 'append-only delete unexpectedly succeeded for %', table_name;
        exception
            when raise_exception then
                if sqlerrm like 'append-only delete unexpectedly succeeded%' then
                    raise;
                end if;
        end;
    end loop;

    if (select count(*) from app.entitlement_grants where grant_id = 'grant:alpha:alice:001') <> 1 then
        raise exception 'entitlement grant was not preserved';
    end if;
end;
$$;

rollback;
