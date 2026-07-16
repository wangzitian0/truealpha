-- The LLM reads only the mart schema, via this role (init.md Section 1, rule 4).
-- statement_timeout solves resource contention, not access control.
-- The application layer ADDITIONALLY forces a LIMIT (1000 rows suggested) on
-- LLM-generated queries — don't rely solely on the database-side setting.

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
grant select on app.publication_policies to app_runtime;
grant select on app.private_research_objects to app_runtime;
revoke select on app.access_audit_metadata from app_runtime;
grant insert on app.authorization_decisions, app.access_audit_events to app_runtime;
revoke all on function app.validate_access_audit_decision_tenant() from public;
grant execute on function app.validate_access_audit_decision_tenant() to app_runtime;

-- Audit readers use a separate role and receive only the administrator-filtered,
-- non-content metadata view; they cannot select either audit base table.
do $$ begin
    create role app_audit_reader nologin;
exception when duplicate_object then null;
end $$;

alter role app_audit_reader set statement_timeout = '5s';

grant usage on schema app to app_audit_reader;
grant select on app.access_audit_metadata to app_audit_reader;
