-- idempotent: true
-- Canonical app schema baseline.

create schema if not exists app;

create table if not exists app.tenants (
 tenant_id text not null,
 recorded_at timestamp with time zone default now() not null,
 constraint tenants_pkey PRIMARY KEY (tenant_id),
 constraint tenants_tenant_id_check CHECK ((length(tenant_id) > 0))
);

create table if not exists app.principals (
 principal_id text not null,
 tenant_id text not null,
 principal_kind text not null,
 recorded_at timestamp with time zone default now() not null,
 constraint principals_pkey PRIMARY KEY (principal_id),
 constraint principals_principal_id_check CHECK ((length(principal_id) > 0)),
 constraint principals_principal_kind_check CHECK ((principal_kind = ANY (ARRAY['member'::text, 'administrator'::text, 'service'::text]))),
 constraint principals_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.conversations (
 conversation_id text not null,
 tenant_id text not null,
 owner_principal_id text not null,
 created_at timestamp with time zone default now() not null,
 constraint conversations_pkey PRIMARY KEY (conversation_id),
 constraint conversations_conversation_id_tenant_id_owner_principal_id_key UNIQUE (conversation_id, tenant_id, owner_principal_id),
 constraint conversations_conversation_id_check CHECK ((length(conversation_id) > 0)),
 constraint conversations_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id),
 constraint conversations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.entitlement_grants (
 grant_id text not null,
 tenant_id text not null,
 principal_id text not null,
 entitlement_id text not null,
 publication_policy_id text not null,
 valid_from timestamp with time zone not null,
 valid_until timestamp with time zone not null,
 recorded_at timestamp with time zone default now() not null,
 constraint entitlement_grants_pkey PRIMARY KEY (grant_id),
 constraint entitlement_grants_check CHECK ((valid_until > valid_from)),
 constraint entitlement_grants_check1 CHECK ((recorded_at >= valid_from)),
 constraint entitlement_grants_entitlement_id_check CHECK ((length(entitlement_id) > 0)),
 constraint entitlement_grants_grant_id_check CHECK ((length(grant_id) > 0)),
 constraint entitlement_grants_publication_policy_id_check CHECK ((length(publication_policy_id) > 0)),
 constraint entitlement_grants_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES app.principals(principal_id),
 constraint entitlement_grants_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.principal_credentials (
 principal_id text not null,
 email text not null,
 hashed_password text not null,
 created_at timestamp with time zone default now() not null,
 updated_at timestamp with time zone default now() not null,
 constraint principal_credentials_pkey PRIMARY KEY (principal_id),
 constraint principal_credentials_email_check CHECK ((length(email) > 0)),
 constraint principal_credentials_hashed_password_check CHECK ((length(hashed_password) > 0)),
 constraint principal_credentials_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES app.principals(principal_id)
);

create table if not exists app.research_documents (
 document_id text not null,
 tenant_id text not null,
 owner_principal_id text not null,
 created_at timestamp with time zone default now() not null,
 constraint research_documents_pkey PRIMARY KEY (document_id),
 constraint research_documents_document_id_tenant_id_owner_principal_id_key UNIQUE (document_id, tenant_id, owner_principal_id),
 constraint research_documents_document_id_check CHECK ((length(document_id) > 0)),
 constraint research_documents_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id),
 constraint research_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.conversation_messages (
 message_id text not null,
 conversation_id text not null,
 tenant_id text not null,
 owner_principal_id text not null,
 role text not null,
 content text not null,
 outcome text,
 created_at timestamp with time zone default now() not null,
 constraint conversation_messages_pkey PRIMARY KEY (message_id),
 constraint conversation_messages_message_id_tenant_id_owner_principal__key UNIQUE (message_id, tenant_id, owner_principal_id),
 constraint conversation_messages_content_check CHECK ((length(content) > 0)),
 constraint conversation_messages_message_id_check CHECK ((length(message_id) > 0)),
 constraint conversation_messages_outcome_check CHECK ((outcome = ANY (ARRAY['result'::text, 'clarification_required'::text, 'unavailable'::text, 'unsupported'::text, 'denied'::text, 'rate_limited'::text, 'invalid'::text]))),
 constraint conversation_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text]))),
 constraint conversation_messages_conversation_id_tenant_id_owner_prin_fkey FOREIGN KEY (conversation_id, tenant_id, owner_principal_id) REFERENCES app.conversations(conversation_id, tenant_id, owner_principal_id),
 constraint conversation_messages_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id),
 constraint conversation_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.research_document_revisions (
 revision_id text not null,
 document_id text not null,
 tenant_id text not null,
 owner_principal_id text not null,
 source_artifact_id text not null,
 artifact_sha256 text not null,
 artifact_byte_length bigint not null,
 artifact_content_type text not null,
 object_key text not null,
 created_at timestamp with time zone default now() not null,
 constraint research_document_revisions_pkey PRIMARY KEY (revision_id),
 constraint research_document_revisions_revision_id_document_id_tenant__key UNIQUE (revision_id, document_id, tenant_id, owner_principal_id),
 constraint research_document_revisions_artifact_byte_length_check CHECK ((artifact_byte_length >= 0)),
 constraint research_document_revisions_artifact_content_type_check CHECK ((length(artifact_content_type) > 0)),
 constraint research_document_revisions_artifact_sha256_check CHECK ((artifact_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint research_document_revisions_object_key_check CHECK ((length(object_key) > 0)),
 constraint research_document_revisions_revision_id_check CHECK ((length(revision_id) > 0)),
 constraint research_document_revisions_source_artifact_id_check CHECK ((length(source_artifact_id) > 0)),
 constraint research_document_revisions_document_id_tenant_id_owner_pr_fkey FOREIGN KEY (document_id, tenant_id, owner_principal_id) REFERENCES app.research_documents(document_id, tenant_id, owner_principal_id),
 constraint research_document_revisions_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id),
 constraint research_document_revisions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

create table if not exists app.research_document_tombstones (
 tombstone_id text not null,
 document_id text not null,
 tenant_id text not null,
 owner_principal_id text not null,
 created_at timestamp with time zone default now() not null,
 constraint research_document_tombstones_pkey PRIMARY KEY (tombstone_id),
 constraint research_document_tombstones_document_id_key UNIQUE (document_id),
 constraint research_document_tombstones_tombstone_id_check CHECK ((length(tombstone_id) > 0)),
 constraint research_document_tombstones_document_id_tenant_id_owner_p_fkey FOREIGN KEY (document_id, tenant_id, owner_principal_id) REFERENCES app.research_documents(document_id, tenant_id, owner_principal_id),
 constraint research_document_tombstones_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES app.principals(principal_id),
 constraint research_document_tombstones_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id)
);

CREATE OR REPLACE FUNCTION app.reject_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'app identity, policy, and audit records are append-only; insert a new event';
end;
$function$;
CREATE OR REPLACE FUNCTION app.touch_principal_credentials_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    new.updated_at := now();
    return new;
end;
$function$;

do $$ begin if to_regclass('app.conversation_audit_metadata') is null then
execute $vc$create or replace view app.conversation_audit_metadata as
 SELECT c.tenant_id,
    c.conversation_id,
    c.owner_principal_id,
    c.created_at AS conversation_created_at,
    count(m.message_id) AS message_count,
    max(m.created_at) AS last_message_at
   FROM app.conversations c
     LEFT JOIN app.conversation_messages m ON m.conversation_id = c.conversation_id
  WHERE c.tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text) AND (EXISTS ( SELECT 1
           FROM app.principals reader
          WHERE reader.principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text) AND reader.tenant_id = c.tenant_id AND reader.principal_kind = 'administrator'::text))
  GROUP BY c.tenant_id, c.conversation_id, c.owner_principal_id, c.created_at$vc$;
end if; end $$;
do $$ begin if to_regclass('app.document_audit_metadata') is null then
execute $vc$create or replace view app.document_audit_metadata as
 SELECT d.tenant_id,
    d.document_id,
    d.owner_principal_id,
    d.created_at AS document_created_at,
    count(r.revision_id) AS revision_count,
    max(r.created_at) AS last_revision_at,
    t.document_id IS NOT NULL AS is_tombstoned
   FROM app.research_documents d
     LEFT JOIN app.research_document_revisions r ON r.document_id = d.document_id
     LEFT JOIN app.research_document_tombstones t ON t.document_id = d.document_id
  WHERE d.tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text) AND (EXISTS ( SELECT 1
           FROM app.principals reader
          WHERE reader.principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text) AND reader.tenant_id = d.tenant_id AND reader.principal_kind = 'administrator'::text))
  GROUP BY d.tenant_id, d.document_id, d.owner_principal_id, d.created_at, t.document_id$vc$;
end if; end $$;

do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.conversation_messages'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '539cde63dc71f9abcd494fe78ba2a3a4') then drop trigger if exists trg_conversation_messages_append_only on app.conversation_messages; CREATE TRIGGER trg_conversation_messages_append_only BEFORE DELETE OR UPDATE ON app.conversation_messages FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.entitlement_grants'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '048ed0aba0a5fc7d693e4702993a61e2') then drop trigger if exists trg_entitlement_grants_append_only on app.entitlement_grants; CREATE TRIGGER trg_entitlement_grants_append_only BEFORE DELETE OR UPDATE ON app.entitlement_grants FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.principal_credentials'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '21a25999fa9aae01e02a421364744028') then drop trigger if exists trg_principal_credentials_touch on app.principal_credentials; CREATE TRIGGER trg_principal_credentials_touch BEFORE UPDATE ON app.principal_credentials FOR EACH ROW EXECUTE FUNCTION app.touch_principal_credentials_updated_at(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.principals'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '97a378d50d589530b576d03ba5607be6') then drop trigger if exists trg_principals_append_only on app.principals; CREATE TRIGGER trg_principals_append_only BEFORE DELETE OR UPDATE ON app.principals FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.research_document_revisions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '22dc8ddb784a0deaf0ae26882df03e66') then drop trigger if exists trg_research_document_revisions_append_only on app.research_document_revisions; CREATE TRIGGER trg_research_document_revisions_append_only BEFORE DELETE OR UPDATE ON app.research_document_revisions FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.research_document_tombstones'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'e0037fa6e1d641cef08e59734f393360') then drop trigger if exists trg_research_document_tombstones_append_only on app.research_document_tombstones; CREATE TRIGGER trg_research_document_tombstones_append_only BEFORE DELETE OR UPDATE ON app.research_document_tombstones FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'app.tenants'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'fbbf4c5bbfb27354122774ceca0623f7') then drop trigger if exists trg_tenants_append_only on app.tenants; CREATE TRIGGER trg_tenants_append_only BEFORE DELETE OR UPDATE ON app.tenants FOR EACH ROW EXECUTE FUNCTION app.reject_mutation(); end if; end $$;

do $$ begin if to_regclass('app.idx_conversation_messages_conversation') is null then CREATE INDEX idx_conversation_messages_conversation ON app.conversation_messages USING btree (conversation_id, created_at); end if; end $$;
do $$ begin if to_regclass('app.idx_principal_credentials_email_lower') is null then CREATE UNIQUE INDEX idx_principal_credentials_email_lower ON app.principal_credentials USING btree (lower(email)); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_document') is null then CREATE INDEX idx_research_document_revisions_document ON app.research_document_revisions USING btree (document_id, created_at); end if; end $$;
do $$ begin if to_regclass('app.idx_principals_tenant') is null then CREATE INDEX idx_principals_tenant ON app.principals USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversations_tenant') is null then CREATE INDEX idx_conversations_tenant ON app.conversations USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversations_owner') is null then CREATE INDEX idx_conversations_owner ON app.conversations USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_conv_tenant_owner') is null then CREATE INDEX idx_conversation_messages_conv_tenant_owner ON app.conversation_messages USING btree (conversation_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_tenant') is null then CREATE INDEX idx_conversation_messages_tenant ON app.conversation_messages USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_owner') is null then CREATE INDEX idx_conversation_messages_owner ON app.conversation_messages USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_entitlement_grants_tenant') is null then CREATE INDEX idx_entitlement_grants_tenant ON app.entitlement_grants USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_entitlement_grants_principal') is null then CREATE INDEX idx_entitlement_grants_principal ON app.entitlement_grants USING btree (principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_documents_tenant') is null then CREATE INDEX idx_research_documents_tenant ON app.research_documents USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_documents_owner') is null then CREATE INDEX idx_research_documents_owner ON app.research_documents USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_doc_tenant_owner') is null then CREATE INDEX idx_research_document_revisions_doc_tenant_owner ON app.research_document_revisions USING btree (document_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_tenant') is null then CREATE INDEX idx_research_document_revisions_tenant ON app.research_document_revisions USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_owner') is null then CREATE INDEX idx_research_document_revisions_owner ON app.research_document_revisions USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_tombstones_tenant') is null then CREATE INDEX idx_research_document_tombstones_tenant ON app.research_document_tombstones USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_tombstones_owner') is null then CREATE INDEX idx_research_document_tombstones_owner ON app.research_document_tombstones USING btree (owner_principal_id); end if; end $$;

do $$ begin
    if not exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relname = 'conversation_messages' and c.relrowsecurity and c.relforcerowsecurity
    ) then
        alter table app.conversation_messages enable row level security;
        alter table app.conversation_messages force row level security;
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relname = 'conversations' and c.relrowsecurity and c.relforcerowsecurity
    ) then
        alter table app.conversations enable row level security;
        alter table app.conversations force row level security;
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relname = 'research_document_revisions' and c.relrowsecurity and c.relforcerowsecurity
    ) then
        alter table app.research_document_revisions enable row level security;
        alter table app.research_document_revisions force row level security;
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relname = 'research_document_tombstones' and c.relrowsecurity and c.relforcerowsecurity
    ) then
        alter table app.research_document_tombstones enable row level security;
        alter table app.research_document_tombstones force row level security;
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relname = 'research_documents' and c.relrowsecurity and c.relforcerowsecurity
    ) then
        alter table app.research_documents enable row level security;
        alter table app.research_documents force row level security;
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'app' and tablename = 'conversation_messages' and policyname = 'conversation_messages_owner_isolation'
    ) then
        create policy conversation_messages_owner_isolation on app.conversation_messages as permissive for ALL to public using (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)))) with check (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'app' and tablename = 'conversations' and policyname = 'conversations_owner_isolation'
    ) then
        create policy conversations_owner_isolation on app.conversations as permissive for ALL to public using (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)))) with check (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'app' and tablename = 'research_document_revisions' and policyname = 'research_document_revisions_owner_isolation'
    ) then
        create policy research_document_revisions_owner_isolation on app.research_document_revisions as permissive for ALL to public using (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)))) with check (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'app' and tablename = 'research_document_tombstones' and policyname = 'research_document_tombstones_owner_isolation'
    ) then
        create policy research_document_tombstones_owner_isolation on app.research_document_tombstones as permissive for ALL to public using (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)))) with check (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));
    end if;
end $$;
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'app' and tablename = 'research_documents' and policyname = 'research_documents_owner_isolation'
    ) then
        create policy research_documents_owner_isolation on app.research_documents as permissive for ALL to public using (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)))) with check (((tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (owner_principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text))));
    end if;
end $$;
