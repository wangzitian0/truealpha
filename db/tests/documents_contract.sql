-- #373: owner-scoped research document lifecycle + download tickets.
-- Run as: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/documents_contract.sql
-- Everything happens inside one transaction and rolls back at the end.

begin;

-- --- schema shape ---
do $$
begin
    if to_regclass('app.research_documents') is null
       or to_regclass('app.research_document_revisions') is null
       or to_regclass('app.research_document_tombstones') is null
       or to_regclass('app.research_document_download_tickets') is null
       or to_regclass('app.document_audit_metadata') is null then
        raise exception 'document lifecycle storage boundary is incomplete';
    end if;
    if not exists (
        select 1 from pg_class
        where oid = 'app.research_documents'::regclass and relrowsecurity and relforcerowsecurity
    ) or not exists (
        select 1 from pg_class
        where oid = 'app.research_document_revisions'::regclass and relrowsecurity and relforcerowsecurity
    ) or not exists (
        select 1 from pg_class
        where oid = 'app.research_document_tombstones'::regclass and relrowsecurity and relforcerowsecurity
    ) or not exists (
        select 1 from pg_class
        where oid = 'app.research_document_download_tickets'::regclass and relrowsecurity and relforcerowsecurity
    ) then
        raise exception 'all four document tables must force row-level security';
    end if;
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'app' and table_name = 'document_audit_metadata'
          and column_name in ('object_key', 'source_artifact_id', 'artifact_sha256')
    ) then
        raise exception 'document audit metadata must not expose artifact bytes or the private locator';
    end if;
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'app' and table_name = 'research_document_revisions'
          and column_name = 'object_key'
    ) is false then
        raise exception 'object_key must exist on revisions (it is the private locator, not absent from storage)';
    end if;
    if has_table_privilege('app_runtime', 'app.document_audit_metadata', 'select') then
        raise exception 'ordinary app runtime must not read the administrator audit projection';
    end if;
    if not has_table_privilege('app_audit_reader', 'app.document_audit_metadata', 'select') then
        raise exception 'app_audit_reader must read the administrator audit projection';
    end if;
end;
$$;

-- --- fixtures: two tenants' worth of principals, one document + revision each ---
insert into app.tenants (tenant_id) values ('tenant:alpha'), ('tenant:beta') on conflict do nothing;
insert into app.principals (principal_id, tenant_id, principal_kind) values
    ('principal:alpha:alice', 'tenant:alpha', 'member'),
    ('principal:alpha:admin', 'tenant:alpha', 'administrator'),
    ('principal:beta:bob', 'tenant:beta', 'member')
on conflict do nothing;

insert into app.research_documents (document_id, tenant_id, owner_principal_id, created_at) values
    ('document:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice', now()),
    ('document:beta:bob-1', 'tenant:beta', 'principal:beta:bob', now());

insert into app.research_document_revisions (
    revision_id, document_id, tenant_id, owner_principal_id,
    source_artifact_id, artifact_sha256, artifact_byte_length, artifact_content_type, object_key, created_at
) values (
    'revision:alpha:alice-1', 'document:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice',
    'report:' || repeat('a', 64), repeat('a', 64), 1024, 'application/json',
    'documents/tenant:alpha/alice/revision:alpha:alice-1', now()
), (
    'revision:beta:bob-1', 'document:beta:bob-1', 'tenant:beta', 'principal:beta:bob',
    'report:' || repeat('b', 64), repeat('b', 64), 2048, 'application/json',
    'documents/tenant:beta/bob/revision:beta:bob-1', now()
);

-- --- append-only: revisions/tombstones reject update and delete ---
do $$
begin
    begin
        update app.research_document_revisions set artifact_byte_length = 999
            where revision_id = 'revision:alpha:alice-1';
        raise exception 'document revision update unexpectedly succeeded';
    exception
        when insufficient_privilege then null;
        when raise_exception then
            if sqlerrm = 'document revision update unexpectedly succeeded' then raise; end if;
    end;
    begin
        delete from app.research_document_revisions where revision_id = 'revision:alpha:alice-1';
        raise exception 'document revision delete unexpectedly succeeded';
    exception
        when insufficient_privilege then null;
        when raise_exception then
            if sqlerrm = 'document revision delete unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

-- --- owner isolation: alice sees only her own document/revision ---
set local role app_runtime;
select set_config('truealpha.tenant_id', 'tenant:alpha', true);
select set_config('truealpha.principal_id', 'principal:alpha:alice', true);

do $$
declare
    own_documents integer;
    own_revisions integer;
    cross_tenant_documents integer;
    guessed_id_hit integer;
begin
    select count(*) into own_documents from app.research_documents;
    select count(*) into own_revisions from app.research_document_revisions;
    select count(*) into cross_tenant_documents from app.research_documents where tenant_id = 'tenant:beta';
    select count(*) into guessed_id_hit from app.research_documents where document_id = 'document:beta:bob-1';

    if own_documents <> 1 then
        raise exception 'alice must see exactly her own document through RLS, saw %', own_documents;
    end if;
    if own_revisions <> 1 then
        raise exception 'alice must see exactly her own revision through RLS, saw %', own_revisions;
    end if;
    if cross_tenant_documents <> 0 then
        raise exception 'cross-tenant document filtering by tenant_id bypassed RLS';
    end if;
    if guessed_id_hit <> 0 then
        raise exception 'guessing bob''s exact document_id bypassed RLS (non-enumerating property violated)';
    end if;
end;
$$;

-- alice can insert her own new document, but not one claiming another owner
do $$
begin
    insert into app.research_documents (document_id, tenant_id, owner_principal_id, created_at)
        values ('document:alpha:alice-2', 'tenant:alpha', 'principal:alpha:alice', now());
    begin
        insert into app.research_documents (document_id, tenant_id, owner_principal_id, created_at)
            values ('document:alpha:forged', 'tenant:alpha', 'principal:beta:bob', now());
        raise exception 'insert with a forged owner_principal_id unexpectedly succeeded';
    exception
        when insufficient_privilege then null; -- expected: RLS blocked the forged insert
    end;
end;
$$;

-- a revision cannot bind to another owner's document even though that
-- document_id genuinely exists (the owner-scoped composite FK, not merely
-- RLS on the revision's own row, is what must block this)
do $$
begin
    begin
        insert into app.research_document_revisions (
            revision_id, document_id, tenant_id, owner_principal_id,
            source_artifact_id, artifact_sha256, artifact_byte_length, artifact_content_type, object_key, created_at
        ) values (
            'revision:alpha:forged', 'document:beta:bob-1', 'tenant:alpha', 'principal:alpha:alice',
            'report:' || repeat('c', 64), repeat('c', 64), 10, 'application/json',
            'documents/forged', now()
        );
        raise exception 'a revision binding to another owner''s document unexpectedly succeeded';
    exception
        when insufficient_privilege then null;
        when foreign_key_violation then null; -- expected: no (document_id, tenant_id, owner_principal_id) row matches
    end;
end;
$$;

-- Deliberately no `reset role` here: tombstones and tickets below stay under
-- app_runtime + alice's GUCs, exercising the real owner-scoped RLS surface.

-- --- tombstone: soft-delete makes get/list non-enumerating (repository layer's job) ---
insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id, created_at)
    values ('tombstone:alpha:alice-1', 'document:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice', now());

do $$
begin
    begin
        insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id, created_at)
            values ('tombstone:alpha:alice-1-again', 'document:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice', now());
        raise exception 'a second tombstone for the same document unexpectedly succeeded';
    exception
        when unique_violation then null; -- expected: at most one tombstone per document
    end;
    begin
        update app.research_document_tombstones set created_at = now() where tombstone_id = 'tombstone:alpha:alice-1';
        raise exception 'tombstone update unexpectedly succeeded';
    exception
        when insufficient_privilege then null;
        when raise_exception then
            if sqlerrm = 'tombstone update unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

-- --- download tickets: single redemption, no other field mutable ---
insert into app.research_document_download_tickets (
    ticket_id, document_id, revision_id, tenant_id, owner_principal_id, expires_at, created_at
) values (
    'ticket:alpha:1', 'document:alpha:alice-1', 'revision:alpha:alice-1', 'tenant:alpha', 'principal:alpha:alice',
    now() + interval '10 minutes', now()
);

do $$
declare
    first_redemption integer;
    second_redemption integer;
begin
    update app.research_document_download_tickets
        set redeemed_at = now()
        where ticket_id = 'ticket:alpha:1' and redeemed_at is null and expires_at > now();
    get diagnostics first_redemption = row_count;
    if first_redemption <> 1 then
        raise exception 'first redemption of a fresh ticket must affect exactly one row, affected %', first_redemption;
    end if;

    update app.research_document_download_tickets
        set redeemed_at = now()
        where ticket_id = 'ticket:alpha:1' and redeemed_at is null and expires_at > now();
    get diagnostics second_redemption = row_count;
    if second_redemption <> 0 then
        raise exception 'replaying an already-redeemed ticket must affect zero rows (single redemption), affected %', second_redemption;
    end if;
end;
$$;

do $$
begin
    begin
        update app.research_document_download_tickets set expires_at = now() + interval '1 day'
            where ticket_id = 'ticket:alpha:1';
        raise exception 'mutating a field other than redeemed_at unexpectedly succeeded';
    exception
        when insufficient_privilege then null;
        when raise_exception then
            if sqlerrm = 'mutating a field other than redeemed_at unexpectedly succeeded' then raise; end if;
            if sqlerrm <> 'research_document_download_tickets: only redeemed_at may change after creation' then raise; end if;
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
    tombstoned boolean;
begin
    select count(*) into audit_rows from app.document_audit_metadata where tenant_id = 'tenant:alpha';
    if audit_rows < 1 then
        raise exception 'administrator must see the non-content audit projection for owned documents';
    end if;
    select is_tombstoned into tombstoned from app.document_audit_metadata
        where document_id = 'document:alpha:alice-1';
    if tombstoned is not true then
        raise exception 'audit projection must report the tombstoned document as tombstoned';
    end if;
    if exists (select 1 from app.document_audit_metadata where tenant_id = 'tenant:beta') then
        raise exception 'administrator audit projection crossed the tenant boundary';
    end if;
end;
$$;

-- An alpha administrator setting truealpha.tenant_id to beta's tenant must
-- not read beta's audit projection (see #396 PR #406's second review round).
select set_config('truealpha.tenant_id', 'tenant:beta', true);

do $$
declare
    audit_rows integer;
begin
    select count(*) into audit_rows from app.document_audit_metadata;
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
    select count(*) into audit_rows from app.document_audit_metadata;
    if audit_rows <> 0 then
        raise exception 'an ordinary member must not read the administrator audit projection';
    end if;
end;
$$;

reset role;
rollback;
