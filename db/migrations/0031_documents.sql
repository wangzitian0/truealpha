-- Owner-scoped research document lifecycle — see #373 (slice of #235).
-- Mirrors 0030's shape: RLS-isolated per owner via the transaction-local
-- truealpha.tenant_id / truealpha.principal_id GUCs, owner-scoped composite
-- FKs so a child row can never bind to another owner's parent (the #396
-- review lesson), and grants live in db/roles.sql, not here.

create table if not exists app.research_documents (
    document_id        text primary key check (length(document_id) > 0),
    tenant_id          text not null references app.tenants (tenant_id),
    owner_principal_id text not null references app.principals (principal_id),
    created_at         timestamptz not null default now(),
    unique (document_id, tenant_id, owner_principal_id)
);

do $$
begin
    if not exists (
        select 1
        from pg_class
        where oid = 'app.research_documents'::regclass
          and relrowsecurity
          and relforcerowsecurity
    ) then
        alter table app.research_documents enable row level security;
        alter table app.research_documents force row level security;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_policy
        where polrelid = 'app.research_documents'::regclass
          and polname = 'research_documents_owner_isolation'
          and polcmd = '*'
          and polpermissive
          and polroles = '{0}'::oid[]
          and pg_get_expr(polqual, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
          and pg_get_expr(polwithcheck, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
    ) then
        drop policy if exists research_documents_owner_isolation on app.research_documents;
        create policy research_documents_owner_isolation on app.research_documents
            for all
            using (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            )
            with check (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            );
    end if;
end
$$;

-- Immutable per revision: a re-render (source/template change) always
-- inserts a new revision_id rather than altering a prior one, so prior
-- output is provably never rewritten. `object_key` is the private S3
-- locator — server-only; it is deliberately absent from every consumer DTO
-- (Python and TypeScript) and only ever read back inside the repository
-- that redeems a download ticket.
create table if not exists app.research_document_revisions (
    revision_id           text primary key check (length(revision_id) > 0),
    document_id           text not null,
    tenant_id             text not null references app.tenants (tenant_id),
    owner_principal_id    text not null references app.principals (principal_id),
    -- Content-addressed id of the #369/#372 artifact this revision serializes
    -- (e.g. "report:<sha256>", "card:<sha256>"). Informational lineage only:
    -- no FK, since neither report nor card has a persisted table to
    -- reference yet (see #373 PR discussion).
    source_artifact_id    text not null check (length(source_artifact_id) > 0),
    artifact_sha256       text not null check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    artifact_byte_length  bigint not null check (artifact_byte_length >= 0),
    artifact_content_type text not null check (length(artifact_content_type) > 0),
    object_key            text not null check (length(object_key) > 0),
    created_at            timestamptz not null default now(),
    foreign key (document_id, tenant_id, owner_principal_id)
        references app.research_documents (document_id, tenant_id, owner_principal_id),
    -- No separate unique(revision_id, tenant_id, owner_principal_id): with
    -- revision_id already the primary key, any tuple containing it is
    -- trivially unique, so only the 4-column constraint below (which is
    -- what the download ticket's FK actually needs) is worth the index.
    -- Lets a download ticket's FK below pin BOTH document_id and
    -- revision_id together against one real revision row, so a ticket can
    -- never be created for a (document_id, revision_id) pair that doesn't
    -- actually correspond to the same revision.
    unique (revision_id, document_id, tenant_id, owner_principal_id)
    -- Deliberately no unique(object_key): the store is content-addressed
    -- and deduplicates by design (object-store.ts's head-before-put), so
    -- two different revisions — even across different documents for the
    -- same owner — legitimately share one physical object when their bytes
    -- are identical. A uniqueness constraint here would reject exactly the
    -- dedup case it's supposed to allow.
);

do $$
begin
    if to_regclass('app.idx_research_document_revisions_document') is null then
        create index if not exists idx_research_document_revisions_document
            on app.research_document_revisions (document_id, created_at);
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_class
        where oid = 'app.research_document_revisions'::regclass
          and relrowsecurity
          and relforcerowsecurity
    ) then
        alter table app.research_document_revisions enable row level security;
        alter table app.research_document_revisions force row level security;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_policy
        where polrelid = 'app.research_document_revisions'::regclass
          and polname = 'research_document_revisions_owner_isolation'
          and polcmd = '*'
          and polpermissive
          and polroles = '{0}'::oid[]
          and pg_get_expr(polqual, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
          and pg_get_expr(polwithcheck, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
    ) then
        drop policy if exists research_document_revisions_owner_isolation on app.research_document_revisions;
        create policy research_document_revisions_owner_isolation on app.research_document_revisions
            for all
            using (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            )
            with check (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            );
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'app.research_document_revisions'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_research_document_revisions_append_only BEFORE DELETE OR UPDATE ON app.research_document_revisions FOR EACH ROW EXECUTE FUNCTION app.reject_mutation()'
    ) then
        drop trigger if exists trg_research_document_revisions_append_only on app.research_document_revisions;
        create trigger trg_research_document_revisions_append_only
        before update or delete on app.research_document_revisions
        for each row execute function app.reject_mutation();
    end if;
end
$$;

-- Presence of a row is the soft-delete: get/list treat a tombstoned
-- document identically to a nonexistent one (non-enumerating), same
-- pattern as a guessed/foreign conversation_id in 0030.
create table if not exists app.research_document_tombstones (
    tombstone_id        text primary key check (length(tombstone_id) > 0),
    document_id         text not null,
    tenant_id           text not null references app.tenants (tenant_id),
    owner_principal_id  text not null references app.principals (principal_id),
    created_at          timestamptz not null default now(),
    foreign key (document_id, tenant_id, owner_principal_id)
        references app.research_documents (document_id, tenant_id, owner_principal_id),
    -- At most one tombstone per document: a document is deleted or it isn't.
    unique (document_id)
);

do $$
begin
    if not exists (
        select 1
        from pg_class
        where oid = 'app.research_document_tombstones'::regclass
          and relrowsecurity
          and relforcerowsecurity
    ) then
        alter table app.research_document_tombstones enable row level security;
        alter table app.research_document_tombstones force row level security;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_policy
        where polrelid = 'app.research_document_tombstones'::regclass
          and polname = 'research_document_tombstones_owner_isolation'
          and polcmd = '*'
          and polpermissive
          and polroles = '{0}'::oid[]
          and pg_get_expr(polqual, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
          and pg_get_expr(polwithcheck, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
    ) then
        drop policy if exists research_document_tombstones_owner_isolation on app.research_document_tombstones;
        create policy research_document_tombstones_owner_isolation on app.research_document_tombstones
            for all
            using (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            )
            with check (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            );
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'app.research_document_tombstones'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_research_document_tombstones_append_only BEFORE DELETE OR UPDATE ON app.research_document_tombstones FOR EACH ROW EXECUTE FUNCTION app.reject_mutation()'
    ) then
        drop trigger if exists trg_research_document_tombstones_append_only on app.research_document_tombstones;
        create trigger trg_research_document_tombstones_append_only
        before update or delete on app.research_document_tombstones
        for each row execute function app.reject_mutation();
    end if;
end
$$;

-- Short-lived, single-redemption download ticket — structurally identical
-- to 0030's app.clarification_tokens (the closest existing analog): atomic
-- conditional UPDATE at the repository layer enforces single redemption,
-- not a trigger, so a missing/redeemed/expired/cross-owner/tombstoned-
-- document ticket all fail the same WHERE clause indistinguishably.
create table if not exists app.research_document_download_tickets (
    ticket_id           text primary key check (length(ticket_id) > 0),
    document_id         text not null,
    revision_id         text not null,
    tenant_id           text not null references app.tenants (tenant_id),
    owner_principal_id  text not null references app.principals (principal_id),
    expires_at          timestamptz not null,
    redeemed_at         timestamptz,
    created_at          timestamptz not null default now(),
    check (expires_at > created_at),
    check (redeemed_at is null or redeemed_at >= created_at),
    foreign key (document_id, tenant_id, owner_principal_id)
        references app.research_documents (document_id, tenant_id, owner_principal_id),
    -- Pins revision_id AND document_id together against one real revision
    -- row (the 4-column unique constraint above) — not two independent
    -- FKs — so a ticket can never redeem a revision that belongs to a
    -- different document than the one it names, which would otherwise let
    -- a tombstone on the *named* document fail to protect the *actual*
    -- revision being redeemed (or vice versa).
    foreign key (revision_id, document_id, tenant_id, owner_principal_id)
        references app.research_document_revisions (revision_id, document_id, tenant_id, owner_principal_id)
);

do $$
begin
    if not exists (
        select 1
        from pg_class
        where oid = 'app.research_document_download_tickets'::regclass
          and relrowsecurity
          and relforcerowsecurity
    ) then
        alter table app.research_document_download_tickets enable row level security;
        alter table app.research_document_download_tickets force row level security;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_policy
        where polrelid = 'app.research_document_download_tickets'::regclass
          and polname = 'research_document_download_tickets_owner_isolation'
          and polcmd = '*'
          and polpermissive
          and polroles = '{0}'::oid[]
          and pg_get_expr(polqual, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
          and pg_get_expr(polwithcheck, polrelid) is not distinct from '((tenant_id = NULLIF(current_setting(''truealpha.tenant_id''::text, true), ''''::text)) AND (owner_principal_id = NULLIF(current_setting(''truealpha.principal_id''::text, true), ''''::text)))'
    ) then
        drop policy if exists research_document_download_tickets_owner_isolation on app.research_document_download_tickets;
        create policy research_document_download_tickets_owner_isolation on app.research_document_download_tickets
            for all
            using (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            )
            with check (
                tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
                and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
            );
    end if;
end
$$;

create or replace function app.reject_document_ticket_field_tamper()
returns trigger language plpgsql as $$
begin
    if new.document_id is distinct from old.document_id
        or new.revision_id is distinct from old.revision_id
        or new.tenant_id is distinct from old.tenant_id
        or new.owner_principal_id is distinct from old.owner_principal_id
        or new.expires_at is distinct from old.expires_at
        or new.created_at is distinct from old.created_at
    then
        raise exception 'research_document_download_tickets: only redeemed_at may change after creation';
    end if;
    return new;
end;
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'app.research_document_download_tickets'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_research_document_download_tickets_guard_update BEFORE UPDATE ON app.research_document_download_tickets FOR EACH ROW EXECUTE FUNCTION app.reject_document_ticket_field_tamper()'
    ) then
        drop trigger if exists trg_research_document_download_tickets_guard_update on app.research_document_download_tickets;
        create trigger trg_research_document_download_tickets_guard_update
        before update on app.research_document_download_tickets
        for each row execute function app.reject_document_ticket_field_tamper();
    end if;
end
$$;

-- Administrator non-content audit projection: counts/timestamps and
-- tombstone status only, never artifact bytes, source_artifact_id, or the
-- object_key locator. Mirrors 0030's conversation_audit_metadata, including
-- the tenant-scoped administrator check (an administrator's own principals
-- row must belong to the *requested* tenant, not merely exist somewhere —
-- see #396 PR #406's second review round).
do $$
declare
    wanted constant text := $view$
select
    d.tenant_id,
    d.document_id,
    d.owner_principal_id,
    d.created_at as document_created_at,
    count(r.revision_id) as revision_count,
    max(r.created_at) as last_revision_at,
    (t.document_id is not null) as is_tombstoned
from app.research_documents d
left join app.research_document_revisions r on r.document_id = d.document_id
left join app.research_document_tombstones t on t.document_id = d.document_id
where d.tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
  and exists (
    select 1
    from app.principals as reader
    where reader.principal_id = nullif(current_setting('truealpha.principal_id', true), '')
      and reader.tenant_id = d.tenant_id
      and reader.principal_kind = 'administrator'
)
group by d.tenant_id, d.document_id, d.owner_principal_id, d.created_at, t.document_id
$view$;
begin
    -- Boot-lock guard: `create or replace view` is ACCESS EXCLUSIVE on the view and
    -- queues every reader behind it; replace only when the definition differs.
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('app.document_audit_metadata') is null
       or pg_get_viewdef(to_regclass('app.document_audit_metadata'))
          is distinct from pg_get_viewdef('pg_temp.boot_guard_candidate'::regclass)
       or (select reloptions from pg_class where oid = to_regclass('app.document_audit_metadata'))
          is distinct from '{security_barrier=true}'::text[]
    then
        execute 'create or replace view app.document_audit_metadata with (security_barrier = true) as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

-- Grants to app_runtime/app_audit_reader live in db/roles.sql, not here:
-- migrations run before roles.sql creates those roles (see #396/0030).
