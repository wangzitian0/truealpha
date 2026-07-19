-- The LLM reads only the mart schema, via this role (init.md Section 1, rule 4).
-- statement_timeout solves resource contention, not access control.
-- The application layer ADDITIONALLY forces a LIMIT (1000 rows suggested) on
-- typed repository queries; no consumer accepts model-generated SQL.

do $$ begin
    create role mart_readonly nologin;
exception when duplicate_object then null;
end $$;

alter role mart_readonly set statement_timeout = '5s';

grant usage on schema mart to mart_readonly;
grant select on all tables in schema mart to mart_readonly;
alter default privileges in schema mart grant select on tables to mart_readonly;

-- Trusted application repositories assume this group role only after deriving
-- transaction-local tenant and principal context. It has no raw/staging access.
do $$ begin
    create role app_runtime nologin;
exception when duplicate_object then null;
end $$;

alter role app_runtime set statement_timeout = '5s';

grant usage on schema app to app_runtime;
grant select on app.publication_policies, app.publication_policy_sets,
    app.publication_policy_entitlements, app.publication_policy_set_seals to app_runtime;
grant select on app.private_research_objects to app_runtime;
revoke select on app.access_audit_metadata from app_runtime;
grant insert on app.authorization_decisions, app.authorization_decision_grants,
    app.access_audit_events to app_runtime;
revoke all on function app.validate_access_audit_decision_tenant() from public;
revoke all on function app.validate_authorization_decision_policy_set() from public;
revoke all on function app.validate_authorization_decision_grant() from public;
revoke all on function app.validate_authorization_decision_required_grants() from public;
revoke all on function app.validate_publication_policy_entitlement_insert() from public;
revoke all on function app.validate_publication_policy_set_seal() from public;
grant execute on function app.validate_access_audit_decision_tenant() to app_runtime;
grant execute on function app.validate_authorization_decision_policy_set() to app_runtime;
grant execute on function app.validate_authorization_decision_grant() to app_runtime;
grant execute on function app.validate_authorization_decision_required_grants() to app_runtime;

-- Login front door (#368, migration 0029). Login itself runs before any
-- tenant/principal GUC is set (it is what *establishes* that context), so
-- these are plain schema-level grants, not RLS-scoped. principal_credentials
-- carries no research content, so it needs no RLS policy of its own.
grant select, insert, update on app.principal_credentials to app_runtime;
grant select on app.principals, app.tenants to app_runtime;

-- Conversation persistence (#396, migration 0030). RLS-scoped like
-- private_research_objects: app_runtime authenticates as itself, then the
-- transaction-local tenant/principal GUCs (set by withOwnerScopedRuntime)
-- are what the row security policies actually check.
grant select, insert on app.conversations, app.conversation_messages, app.research_gap_requests to app_runtime;
grant select, insert, update (redeemed_at) on app.clarification_tokens to app_runtime;
revoke select on app.conversation_audit_metadata from app_runtime;

-- Document lifecycle (#373, migration 0031). Same RLS-scoped shape as
-- conversations above. Revisions and tombstones are fully append-only (no
-- UPDATE grant at all); tickets get the same redeemed_at-only column grant
-- as clarification_tokens.
grant select, insert on app.research_documents, app.research_document_revisions,
    app.research_document_tombstones to app_runtime;
grant select, insert, update (redeemed_at) on app.research_document_download_tickets to app_runtime;
revoke select on app.document_audit_metadata from app_runtime;

-- Audit readers receive only the administrator-filtered, non-content view.
do $$ begin
    create role app_audit_reader nologin;
exception when duplicate_object then null;
end $$;

alter role app_audit_reader set statement_timeout = '5s';

grant usage on schema app to app_audit_reader;
grant select on app.access_audit_metadata to app_audit_reader;
grant select on app.conversation_audit_metadata to app_audit_reader;
grant select on app.document_audit_metadata to app_audit_reader;
