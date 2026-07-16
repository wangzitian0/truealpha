begin;

do $$
declare
    append_only_trigger_count integer;
    forbidden_audit_column_count integer;
begin
    if to_regnamespace('app') is null then
        raise exception 'app schema is missing';
    end if;
    if to_regclass('app.private_research_objects') is null
       or to_regclass('app.authorization_decisions') is null
       or to_regclass('app.access_audit_events') is null then
        raise exception 'governed access storage boundary is incomplete';
    end if;
    if not exists (
        select 1
        from pg_class
        where oid = 'app.private_research_objects'::regclass
          and relrowsecurity
          and relforcerowsecurity
    ) then
        raise exception 'private research objects must force row-level security';
    end if;
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'app'
          and tablename = 'private_research_objects'
          and policyname = 'private_research_owner_isolation'
    ) then
        raise exception 'private research owner policy is missing';
    end if;

    select count(*) into append_only_trigger_count
    from pg_trigger
    where not tgisinternal
      and tgname like 'trg_%_append_only'
      and tgrelid in (
          'app.tenants'::regclass,
          'app.principals'::regclass,
          'app.tenant_memberships'::regclass,
          'app.entitlement_grants'::regclass,
          'app.grant_revocations'::regclass,
          'app.publication_policies'::regclass,
          'app.private_research_objects'::regclass,
          'app.authorization_decisions'::regclass,
          'app.access_audit_events'::regclass
      );
    if append_only_trigger_count <> 9 then
        raise exception 'all governed access records must be append-only';
    end if;

    select count(*) into forbidden_audit_column_count
    from information_schema.columns
    where table_schema = 'app'
      and table_name = 'access_audit_events'
      and column_name in ('content', 'body', 'payload', 'document_text', 'conversation_text');
    if forbidden_audit_column_count <> 0 then
        raise exception 'access audit metadata must not persist private content';
    end if;

    if has_table_privilege('app_runtime', 'raw.fetches', 'select')
       or has_table_privilege('app_runtime', 'staging.financial_facts', 'select') then
        raise exception 'app runtime must not read raw or staging data';
    end if;
end;
$$;

insert into app.tenants (tenant_id, recorded_at)
values
    ('tenant:alpha', '2026-07-15T00:00:00Z'),
    ('tenant:beta', '2026-07-15T00:00:00Z');

insert into app.principals (principal_id, tenant_id, principal_kind, recorded_at)
values
    ('principal:alpha:alice', 'tenant:alpha', 'member', '2026-07-15T00:00:00Z'),
    ('principal:beta:bob', 'tenant:beta', 'member', '2026-07-15T00:00:00Z');

insert into app.private_research_objects (
    resource_id,
    tenant_id,
    owner_principal_id,
    resource_type,
    object_ref,
    recorded_at
)
values
    (
        'document:alpha:private-001',
        'tenant:alpha',
        'principal:alpha:alice',
        'private_document',
        'object:alpha:001',
        '2026-07-15T00:00:00Z'
    ),
    (
        'document:beta:private-001',
        'tenant:beta',
        'principal:beta:bob',
        'private_document',
        'object:beta:001',
        '2026-07-15T00:00:00Z'
    );

insert into app.authorization_decisions (
    decision_id,
    tenant_id,
    principal_id,
    action,
    resource_id,
    publication_policy_id,
    decision,
    decided_at,
    recorded_at
)
values
    (
        'access-decision:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'tenant:alpha',
        'principal:alpha:alice',
        'read_content',
        'document:alpha:private-001',
        'publication-policy:research:v1',
        'allow',
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    ),
    (
        'access-decision:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'tenant:beta',
        'principal:beta:bob',
        'read_content',
        'document:beta:private-001',
        'publication-policy:research:v1',
        'allow',
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    );

insert into app.access_audit_events (
    audit_event_id,
    decision_id,
    tenant_id,
    principal_id,
    event_kind,
    occurred_at,
    recorded_at
)
values
    (
        'audit-event:alpha:001',
        'access-decision:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'tenant:alpha',
        'principal:alpha:alice',
        'access_allowed',
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    ),
    (
        'audit-event:beta:001',
        'access-decision:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'tenant:beta',
        'principal:beta:bob',
        'access_allowed',
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    );

do $$
begin
    begin
        update app.private_research_objects
        set object_ref = 'object:alpha:replacement'
        where resource_id = 'document:alpha:private-001';
        raise exception 'append-only update unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'append-only update unexpectedly succeeded' then
                raise;
            end if;
    end;
end;
$$;

set local role app_runtime;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    own_count integer;
    cross_tenant_count integer;
    own_audit_count integer;
    cross_tenant_audit_count integer;
begin
    select count(*) into own_count
    from app.private_research_objects
    where resource_id = 'document:alpha:private-001';
    select count(*) into cross_tenant_count
    from app.private_research_objects
    where resource_id = 'document:beta:private-001';
    select count(*) into own_audit_count
    from app.access_audit_metadata
    where audit_event_id = 'audit-event:alpha:001';
    select count(*) into cross_tenant_audit_count
    from app.access_audit_metadata
    where audit_event_id = 'audit-event:beta:001';
    if own_count <> 1 then
        raise exception 'owner must see the private object through RLS';
    end if;
    if cross_tenant_count <> 0 then
        raise exception 'cross-tenant object-ID guessing bypassed RLS';
    end if;
    if own_audit_count <> 1 then
        raise exception 'authorized tenant audit metadata is unreadable';
    end if;
    if cross_tenant_audit_count <> 0 then
        raise exception 'cross-tenant audit metadata is readable';
    end if;
    if has_table_privilege('app_runtime', 'app.authorization_decisions', 'select')
       or has_table_privilege('app_runtime', 'app.access_audit_events', 'select') then
        raise exception 'runtime audit reads must use the restricted metadata view';
    end if;
end;
$$;

reset role;
rollback;
