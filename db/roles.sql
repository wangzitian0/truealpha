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
