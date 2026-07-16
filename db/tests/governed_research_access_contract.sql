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
       or to_regclass('app.authorization_decision_grants') is null
       or to_regclass('app.access_audit_events') is null
       or to_regclass('app.publication_policy_sets') is null
       or to_regclass('app.publication_policy_entitlements') is null
       or to_regclass('app.access_audit_metadata') is null then
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
          'app.authorization_decision_grants'::regclass,
          'app.publication_policy_sets'::regclass,
          'app.publication_policy_entitlements'::regclass,
          'app.access_audit_events'::regclass
      );
    if append_only_trigger_count <> 12 then
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
    if has_table_privilege('app_runtime', 'app.access_audit_metadata', 'select') then
        raise exception 'ordinary app runtime must not read administrator audit metadata';
    end if;
    if not has_table_privilege('app_audit_reader', 'app.access_audit_metadata', 'select')
       or has_table_privilege('app_audit_reader', 'app.authorization_decisions', 'select')
       or has_table_privilege('app_audit_reader', 'app.access_audit_events', 'select') then
        raise exception 'audit reader must receive only the filtered metadata view';
    end if;
end;
$$;

insert into app.tenants (tenant_id, recorded_at)
values
    ('tenant:alpha', '2026-07-15T00:00:00Z'),
    ('tenant:beta', '2026-07-15T00:00:00Z'),
    ('tenant:platform', '2026-07-15T00:00:00Z');

insert into app.principals (principal_id, tenant_id, principal_kind, recorded_at)
values
    ('principal:alpha:alice', 'tenant:alpha', 'member', '2026-07-15T00:00:00Z'),
    ('principal:beta:bob', 'tenant:beta', 'member', '2026-07-15T00:00:00Z'),
    ('principal:platform:admin', 'tenant:platform', 'administrator', '2026-07-15T00:00:00Z');

insert into app.tenant_memberships (
    membership_event_id,
    tenant_id,
    principal_id,
    membership_state,
    effective_at,
    recorded_at
)
values
    (
        'membership-event:alpha:alice:001',
        'tenant:alpha',
        'principal:alpha:alice',
        'granted',
        '2026-07-15T00:00:00Z',
        '2026-07-15T00:00:00Z'
    );

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
values
    (
        'grant:alpha:alice:001',
        'tenant:alpha',
        'principal:alpha:alice',
        'entitlement:research:standard:v1',
        'publication-policy-set:research:v2',
        '2026-07-15T00:00:00Z',
        '2026-07-15T01:00:00Z',
        '2026-07-15T00:00:00Z'
    );

insert into app.grant_revocations (
    revocation_id,
    tenant_id,
    grant_id,
    revoked_at,
    reason_code,
    recorded_at
)
values
    (
        'revocation-event:alpha:alice:001',
        'tenant:alpha',
        'grant:alpha:alice:001',
        '2026-07-15T00:10:00Z',
        'delegation_revoked',
        '2026-07-15T00:10:00Z'
    );

insert into app.publication_policies (
    publication_policy_event_id,
    publication_policy_id,
    publication_class_id,
    permitted,
    successor_policy_id,
    effective_at,
    recorded_at
)
values
    (
        'publication-policy-event:001',
        'publication-policy:research:v1',
        'publication-class:standard:v1',
        true,
        null,
        '2026-07-15T00:00:00Z',
        '2026-07-15T00:00:00Z'
    ),
    (
        'publication-policy-event:002',
        'publication-policy:research:v1',
        'publication-class:standard:v1',
        true,
        'publication-policy:research:v2',
        '2026-07-15T00:20:00Z',
        '2026-07-15T00:20:00Z'
    );

insert into app.publication_policy_sets (
    publication_policy_set_id,
    content_sha256,
    release_manifest_id,
    recorded_at
)
values (
    'publication-policy-set:research:v2',
    '4cc4f0d79486130bda4de3451b56770a5c295881535602b692bbf1cda585cdfd',
    'release-manifest:research:v1',
    '2026-07-15T00:00:00Z'
);

insert into app.publication_policy_entitlements (
    publication_policy_rule_id,
    publication_policy_set_id,
    publication_class_id,
    entitlement_id,
    recorded_at
)
values
    (
        'publication-policy-rule:standard-standard:v2',
        'publication-policy-set:research:v2',
        'publication-class:standard:v1',
        'entitlement:research:standard:v1',
        '2026-07-15T00:00:00Z'
    ),
    (
        'publication-policy-rule:standard-premium:v2',
        'publication-policy-set:research:v2',
        'publication-class:standard:v1',
        'entitlement:research:premium:v1',
        '2026-07-15T00:00:00Z'
    ),
    (
        'publication-policy-rule:restricted-premium:v2',
        'publication-policy-set:research:v2',
        'publication-class:restricted:v1',
        'entitlement:research:premium:v1',
        '2026-07-15T00:00:00Z'
    );

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
        'conversation:alpha:private-001',
        'tenant:alpha',
        'principal:alpha:alice',
        'private_conversation',
        'object:alpha:conversation:001',
        '2026-07-15T00:00:00Z'
    ),
    (
        'document:beta:private-001',
        'tenant:beta',
        'principal:beta:bob',
        'private_document',
        'object:beta:001',
        '2026-07-15T00:00:00Z'
    ),
    (
        'conversation:beta:private-001',
        'tenant:beta',
        'principal:beta:bob',
        'private_conversation',
        'object:beta:conversation:001',
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
    reason_code,
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
        'publication-policy-set:research:v2',
        'allow',
        null,
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    ),
    (
        'access-decision:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'tenant:beta',
        'principal:beta:bob',
        'read_content',
        'document:beta:private-001',
        'publication-policy-set:research:v2',
        'allow',
        null,
        '2026-07-15T00:01:00Z',
        '2026-07-15T00:01:00Z'
    ),
    (
        'access-decision:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        'tenant:alpha',
        'principal:alpha:alice',
        'read_content',
        'conversation:beta:private-001',
        'publication-policy-set:research:v2',
        'deny',
        'tenant_mismatch',
        '2026-07-15T00:06:00Z',
        '2026-07-15T00:06:00Z'
    ),
    (
        'access-decision:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        'tenant:alpha',
        'principal:alpha:alice',
        'read_materialized_result',
        'strategy-result:alpha:standard-001',
        'publication-policy-set:research:v2',
        'allow',
        null,
        '2026-07-15T00:11:00Z',
        '2026-07-15T00:11:00Z'
    );

insert into app.authorization_decision_grants (decision_id, grant_id, recorded_at)
values (
    'access-decision:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'grant:alpha:alice:001',
    '2026-07-15T00:01:00Z'
);

do $$
begin
    begin
        insert into app.authorization_decision_grants (decision_id, grant_id, recorded_at)
        values (
            'access-decision:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            'grant:alpha:alice:001',
            '2026-07-15T00:01:00Z'
        );
        raise exception 'cross-tenant decision grant unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'cross-tenant decision grant unexpectedly succeeded' then
                raise;
            end if;
            if sqlerrm <> 'authorization decision grant identity mismatch' then
                raise;
            end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into app.authorization_decision_grants (decision_id, grant_id, recorded_at)
        values (
            'access-decision:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            'grant:alpha:alice:001',
            '2026-07-15T00:11:00Z'
        );
        raise exception 'revoked decision grant unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'revoked decision grant unexpectedly succeeded' then
                raise;
            end if;
            if sqlerrm <> 'authorization decision grant was not active at decision time' then
                raise;
            end if;
    end;
end;
$$;

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
    ),
    (
        'audit-event:alpha:002',
        'access-decision:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        'tenant:alpha',
        'principal:alpha:alice',
        'access_denied',
        '2026-07-15T00:06:00Z',
        '2026-07-15T00:06:00Z'
    );

do $$
begin
    begin
        insert into app.access_audit_events (
            audit_event_id,
            decision_id,
            tenant_id,
            principal_id,
            event_kind,
            occurred_at,
            recorded_at
        )
        values (
            'audit-event:cross-tenant-invalid',
            'access-decision:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'tenant:beta',
            'principal:alpha:alice',
            'access_allowed',
            '2026-07-15T00:01:00Z',
            '2026-07-15T00:01:00Z'
        );
        raise exception 'cross-tenant audit event unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'cross-tenant audit event unexpectedly succeeded' then
                raise;
            end if;
            if sqlerrm <> 'access audit tenant must match its authorization decision' then
                raise;
            end if;
    end;
end;
$$;

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
        'authorization_decision_grants',
        'publication_policy_sets',
        'publication_policy_entitlements',
        'access_audit_events'
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

    if (select count(*) from app.entitlement_grants where grant_id = 'grant:alpha:alice:001') <> 1
       or (select count(*) from app.grant_revocations where grant_id = 'grant:alpha:alice:001') <> 1 then
        raise exception 'grant-then-revoke history was not preserved';
    end if;
    if (select count(*) from app.publication_policies where publication_policy_id = 'publication-policy:research:v1') <> 2 then
        raise exception 'publication policy supersession history was not preserved';
    end if;
    if (select count(*) from app.access_audit_events where tenant_id = 'tenant:alpha') <> 2 then
        raise exception 'allowed-and-denied access audit history was not preserved';
    end if;
    if (select array_agg(grant_id order by grant_id)
        from app.authorization_decision_grants
        where decision_id = 'access-decision:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
       is distinct from array['grant:alpha:alice:001']::text[] then
        raise exception 'authorization decision did not retain the exact entitlement grant identity';
    end if;
end;
$$;

set local role app_runtime;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    own_count integer;
    cross_tenant_count integer;
begin
    select count(*) into own_count
    from app.private_research_objects
    where tenant_id = 'tenant:alpha';
    select count(*) into cross_tenant_count
    from app.private_research_objects
    where tenant_id = 'tenant:beta';
    if own_count <> 2 then
        raise exception 'owner must see private conversation and document rows through RLS';
    end if;
    if cross_tenant_count <> 0 then
        raise exception 'cross-tenant object-ID guessing bypassed RLS';
    end if;
    if has_table_privilege('app_runtime', 'app.authorization_decisions', 'select')
       or has_table_privilege('app_runtime', 'app.access_audit_events', 'select') then
        raise exception 'app runtime must not read immutable audit base tables';
    end if;
end;
$$;

reset role;

set local role app_audit_reader;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:platform:admin', true);

do $$
declare
    audit_count integer;
    cross_tenant_count integer;
begin
    select count(*) into audit_count from app.access_audit_metadata;
    select count(*) into cross_tenant_count
    from app.access_audit_metadata
    where tenant_id = 'tenant:beta';
    if audit_count <> 2 then
        raise exception 'authorized administrator must read target-tenant non-content audit metadata';
    end if;
    if cross_tenant_count <> 0 then
        raise exception 'administrator audit view crossed the configured tenant boundary';
    end if;
end;
$$;

select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    audit_count integer;
begin
    select count(*) into audit_count from app.access_audit_metadata;
    if audit_count <> 0 then
        raise exception 'ordinary member must not read administrator audit metadata';
    end if;
end;
$$;

reset role;
rollback;
