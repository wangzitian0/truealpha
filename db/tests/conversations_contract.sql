-- #396: owner-scoped conversation persistence.
-- Run as: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/conversations_contract.sql
-- Everything happens inside one transaction and rolls back at the end.

begin;

-- --- schema shape ---
do $$
begin
    if to_regclass('app.conversations') is null
       or to_regclass('app.conversation_messages') is null
       or to_regclass('app.conversation_audit_metadata') is null then
        raise exception 'conversation persistence storage boundary is incomplete';
    end if;
    if not exists (
        select 1 from pg_class
        where oid = 'app.conversations'::regclass and relrowsecurity and relforcerowsecurity
    ) or not exists (
        select 1 from pg_class
        where oid = 'app.conversation_messages'::regclass and relrowsecurity and relforcerowsecurity
    ) then
        raise exception 'all conversation tables must force row-level security';
    end if;
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'app' and table_name = 'conversation_audit_metadata'
          and column_name in ('content', 'prompt_text')
    ) then
        raise exception 'conversation audit metadata must not expose message/prompt content';
    end if;
    if has_table_privilege('app_runtime', 'app.conversation_audit_metadata', 'select') then
        raise exception 'ordinary app runtime must not read the administrator audit projection';
    end if;
    if not has_table_privilege('app_audit_reader', 'app.conversation_audit_metadata', 'select') then
        raise exception 'app_audit_reader must read the administrator audit projection';
    end if;
end;
$$;

-- --- fixtures: two tenants' worth of principals, one conversation + message each ---
insert into app.tenants (tenant_id) values ('tenant:alpha'), ('tenant:beta') on conflict do nothing;
insert into app.principals (principal_id, tenant_id, principal_kind) values
    ('principal:alpha:alice', 'tenant:alpha', 'member'),
    ('principal:alpha:admin', 'tenant:alpha', 'administrator'),
    ('principal:beta:bob', 'tenant:beta', 'member')
on conflict do nothing;

insert into app.conversations (conversation_id, tenant_id, owner_principal_id, created_at) values
    ('conversation:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice', now()),
    ('conversation:beta:bob-1', 'tenant:beta', 'principal:beta:bob', now());

insert into app.conversation_messages (
    message_id, conversation_id, tenant_id, owner_principal_id, role, content, outcome, created_at
) values (
    'message:alpha:alice-1', 'conversation:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice',
    'assistant', 'ADM PEG trend is improving', 'result', now()
), (
    'message:beta:bob-1', 'conversation:beta:bob-1', 'tenant:beta', 'principal:beta:bob',
    'assistant', 'an answer only bob received', 'result', now()
);

-- --- append-only: no edit, no delete ---
do $$
begin
    begin
        update app.conversation_messages set content = 'edited' where message_id = 'message:alpha:alice-1';
        raise exception 'conversation message update unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'conversation message update unexpectedly succeeded' then raise; end if;
    end;
    begin
        delete from app.conversation_messages where message_id = 'message:alpha:alice-1';
        raise exception 'conversation message delete unexpectedly succeeded';
    exception
        when raise_exception then
            if sqlerrm = 'conversation message delete unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

-- --- owner isolation: alice sees only her own conversation/messages ---
set local role app_runtime;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    own_conversations integer;
    own_messages integer;
    cross_tenant_conversations integer;
    guessed_id_hit integer;
begin
    select count(*) into own_conversations from app.conversations;
    select count(*) into own_messages from app.conversation_messages;
    select count(*) into cross_tenant_conversations from app.conversations where tenant_id = 'tenant:beta';
    select count(*) into guessed_id_hit from app.conversations where conversation_id = 'conversation:beta:bob-1';

    if own_conversations <> 1 then
        raise exception 'alice must see exactly her own conversation through RLS, saw %', own_conversations;
    end if;
    if own_messages <> 1 then
        raise exception 'alice must see exactly her own message through RLS, saw %', own_messages;
    end if;
    if cross_tenant_conversations <> 0 then
        raise exception 'cross-tenant conversation filtering by tenant_id bypassed RLS';
    end if;
    if guessed_id_hit <> 0 then
        raise exception 'guessing bob''s exact conversation_id bypassed RLS (non-enumerating property violated)';
    end if;
end;
$$;

-- alice can insert her own new conversation, but not one claiming another owner
do $$
begin
    insert into app.conversations (conversation_id, tenant_id, owner_principal_id, created_at)
        values ('conversation:alpha:alice-2', 'tenant:alpha', 'principal:alpha:alice', now());
    begin
        insert into app.conversations (conversation_id, tenant_id, owner_principal_id, created_at)
            values ('conversation:alpha:forged', 'tenant:alpha', 'principal:beta:bob', now());
        raise exception 'insert with a forged owner_principal_id unexpectedly succeeded';
    exception
        -- Postgres raises its own "new row violates row-level security policy" error
        -- (SQLSTATE 42501 / insufficient_privilege) for the WITH CHECK failure — this is
        -- not the raise_exception (P0001) class our own sentinel/trigger errors use.
        when insufficient_privilege then null; -- expected: RLS blocked the forged insert
    end;
end;
$$;

-- --- administrator audit projection: aggregate only, tenant-scoped, denied to non-admins ---
set local role app_audit_reader;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:admin', true);

do $$
declare
    audit_rows integer;
begin
    select count(*) into audit_rows from app.conversation_audit_metadata where tenant_id = 'tenant:alpha';
    if audit_rows < 1 then
        raise exception 'administrator must see the non-content audit projection for owned conversations';
    end if;
    if exists (select 1 from app.conversation_audit_metadata where tenant_id = 'tenant:beta') then
        raise exception 'administrator audit projection crossed the tenant boundary';
    end if;
end;
$$;

-- An alpha administrator setting truealpha.tenant_id to beta's tenant must not
-- be able to read beta's audit projection: the view's own administrator check
-- must verify the reader's row belongs to the *requested* tenant, not merely
-- that some row for that principal_id is kind='administrator' somewhere.
select set_config('truealpha.tenant_id', 'tenant:beta', true);

do $$
declare
    audit_rows integer;
begin
    select count(*) into audit_rows from app.conversation_audit_metadata;
    if audit_rows <> 0 then
        raise exception 'an alpha administrator switching the tenant GUC to beta must not read beta''s audit projection, saw %', audit_rows;
    end if;
end;
$$;

select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    audit_rows integer;
begin
    select count(*) into audit_rows from app.conversation_audit_metadata;
    if audit_rows <> 0 then
        raise exception 'an ordinary member must not read the administrator audit projection';
    end if;
end;
$$;

reset role;
rollback;
