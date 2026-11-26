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
do $$
declare
    tbl text;
begin
    foreach tbl in array array['mart.current_pointer', 'mart.current_pointer_head'] loop
        if to_regclass(tbl) is not null and has_table_privilege('mart_readonly', tbl, 'select') then
            execute format('revoke select on %s from mart_readonly', tbl);
        end if;
    end loop;
end $$;

-- Trusted application repositories assume this group role only after deriving
-- transaction-local tenant and principal context. It has no raw/staging access.
do $$ begin
    create role app_runtime nologin;
exception when duplicate_object then null;
end $$;

alter role app_runtime set statement_timeout = '5s';

grant usage on schema app to app_runtime;
-- Staging write exceptions: DB-mediated pipeline trigger and head rollback
-- requests. USAGE on staging plus these two tables exposes nothing else;
-- consume-UPDATEs stay with the data-engine/migration role, and each table's
-- triggers enforce append-only and consume-once invariants.
grant usage on schema staging to app_runtime;
grant select, insert on staging.pipeline_trigger_requests to app_runtime;
grant select, insert on staging.head_rollback_requests to app_runtime;

-- Login front door (#368). Login itself runs before any
-- tenant/principal GUC is set (it is what *establishes* that context), so
-- these are plain schema-level grants, not RLS-scoped. principal_credentials
-- carries no research content, so it needs no RLS policy of its own.
grant select, insert, update on app.principal_credentials to app_runtime;
grant select on app.principals, app.tenants to app_runtime;

-- Conversation persistence (#396). RLS-scoped: app_runtime
-- authenticates as itself, then the transaction-local tenant/principal GUCs
-- (set by withOwnerScopedRuntime) are what the row security policies actually check.
grant select, insert on app.conversations, app.conversation_messages to app_runtime;
revoke select on app.conversation_audit_metadata from app_runtime;

-- Document lifecycle (#373). Same RLS-scoped shape as
-- conversations above. Revisions and tombstones are fully append-only (no
-- UPDATE grant at all).
grant select, insert on app.research_documents, app.research_document_revisions,
    app.research_document_tombstones to app_runtime;
revoke select on app.document_audit_metadata from app_runtime;

-- Audit readers receive only the administrator-filtered, non-content view.
do $$ begin
    create role app_audit_reader nologin;
exception when duplicate_object then null;
end $$;

alter role app_audit_reader set statement_timeout = '5s';

grant usage on schema app to app_audit_reader;
grant select on app.conversation_audit_metadata to app_audit_reader;
grant select on app.document_audit_metadata to app_audit_reader;

-- Scoped runtime login for app-web + llm-service (#432). Since Stage B both containers'
-- DATABASE_URL logs in as this role (llm_service.config DEFAULT_DATABASE_URL). Migrations
-- keep using the superuser via MIGRATIONS_DATABASE_URL (db/apply_migrations.sh), never this role.
do $$ begin
    create role app_service_login login noinherit;
exception when duplicate_object then null;
end $$;

do $$ begin
    if exists (select 1 from pg_roles where rolname = 'app_service_login' and rolinherit) then
        alter role app_service_login noinherit;
    end if;
end $$;

alter role app_service_login set statement_timeout = '5s';
-- API reads return in milliseconds. JIT compiled for seconds whenever a plan's cost estimate
-- drifted above jit_above_cost: /api/v1/entities/holdings ran 0.02 s with JIT off and 2.8 s
-- with it on, and timed out under the 5 s limit above (#1707). Role settings apply at login.
alter role app_service_login set jit = off;
grant mart_readonly to app_service_login;
grant app_runtime to app_service_login;

-- Password is provisioned out-of-band via Vault (APP_SERVICE_DB_PASSWORD, rendered by
-- infra2's secrets.ctmpl into the same env apply_migrations.sh runs with) and applied
-- here so a Vault-side rotation takes effect on the next migration run without a manual
-- psql step. Since #984 every environment reaches this file through
-- db/apply_migrations.sh, which always passes `-v app_service_db_password=...` — empty
-- wherever APP_SERVICE_DB_PASSWORD is unset (such as local and CI). The
-- `where nullif(...) is not null` below is what makes that a complete no-op there:
-- only environments that set APP_SERVICE_DB_PASSWORD update the login password.
-- The ALTER ROLE is built via \gexec rather than a dollar-quoted DO block: psql does
-- NOT interpolate `:'var'` inside `$$...$$` bodies (verified empirically -- it sends
-- the literal `:'...'` text and Postgres's parser rejects it), so the substitution has
-- to happen in a plain top-level statement instead. generate_password() (common/env.py)
-- emits ascii-letters/digits only, so no quoting/escaping edge cases reach the %L
-- literal psql produces.
\if :{?app_service_db_password}
select format('alter role app_service_login password %L', nullif(:'app_service_db_password', ''))
  where nullif(:'app_service_db_password', '') is not null \gexec
\endif

-- The /admin ops and /admin/datahub surfaces read run history, pointer
-- freshness, operational reports, and per-source quota burn. No existing consumer role
-- may touch dagster/raw (mart_readonly is mart-only; app_runtime is app-only plus the
-- two request tables), so the ops surface gets its own nologin role with explicit
-- read-only grants below. Granted to app_service_login so the #432 cutover works unchanged.
do $$ begin
    create role app_ops_reader nologin;
exception when duplicate_object then null;
end $$;

alter role app_ops_reader set statement_timeout = '5s';
grant usage on schema dagster to app_ops_reader;
-- dagster.runs is created by Dagster's own instance bootstrap at runtime, not
-- by our migrations — on a fresh database (CI) it does not exist yet, so the
-- grant is conditional; deployed environments always have it by the time this
-- runs, and re-running roles.sql after bootstrap picks it up.
do $$ begin
    if exists (select 1 from information_schema.tables
               where table_schema = 'dagster' and table_name = 'runs') then
        grant select on dagster.runs to app_ops_reader;
    end if;
    -- /admin ops shows the first error line of the newest failed run of each job. The event
    -- log holds that text. The administrator-only page is the one reader, and the text never reaches mart.
    if exists (select 1 from information_schema.tables
               where table_schema = 'dagster' and table_name = 'event_logs') then
        grant select on dagster.event_logs to app_ops_reader;
    end if;
end $$;
grant usage on schema raw to app_ops_reader;
grant select on raw.fetches to app_ops_reader;
-- #539 universe provenance page: the constituent plane + governed heads, read-only.
grant usage on schema staging to app_ops_reader;
grant select on staging.etf_constituent_facts to app_ops_reader;
grant select on staging.accepted_rulesets to app_ops_reader;
grant select on staging.accepted_ruleset_head to app_ops_reader;
grant select on staging.contract_objects to app_ops_reader;
-- #729: the external call ledger — /admin/datahub's Capacity and Traffic sections read
-- every vendor request (success or failure) and join it to the landed raw.fetches row.
grant select on staging.api_call_ledger to app_ops_reader;
grant select on staging.model_invocations, staging.ingestion_health_log, staging.pipeline_trigger_requests, staging.head_rollback_requests to app_ops_reader;
grant usage on schema mart to app_ops_reader;
grant select on mart.current_pointer_head to app_ops_reader;
-- #1062: the served head, with its age, limit, label and availability.
-- Also its two tables and the view of the environments that hold heads.
-- Consumers read mart.served_head, not mart.current_pointer_head. mart_readonly already
-- holds select on every mart relation; the explicit lines keep the grant visible here.
grant select on mart.served_head to mart_readonly;
grant select on mart.served_head to app_ops_reader;
grant select on mart.served_artifact to mart_readonly;
grant select on mart.served_artifact to app_ops_reader;
grant select on mart.freshness_limit to mart_readonly;
grant select on mart.freshness_limit to app_ops_reader;
grant select on mart.served_head_environments to mart_readonly;
grant select on mart.served_head_environments to app_ops_reader;
grant select on mart.strategy_run_capture_bindings to app_ops_reader;
grant select on mart.strategy_runs, mart.strategy_decisions, mart.backtest_runs to app_ops_reader;
-- #712: the data-engine build behind the newest run, for the /admin deployments card.
grant select on mart.data_engine_identity to app_ops_reader;
-- #756: the database environment identity, for admin / status readers.
grant select on mart.environment_identity to app_ops_reader;
-- #998: a SECURITY DEFINER routine runs with its OWNER's privileges, and PostgreSQL grants
-- EXECUTE on every new routine to PUBLIC. Revoke PUBLIC from every SECURITY DEFINER
-- routine in staging so neither app_runtime nor app_ops_reader can execute staging
-- definer helpers directly. Set-based views (like mart.entity_identity after #1344)
-- do not grant EXECUTE to mart_readonly or app_ops_reader;
-- db/tests/role_boundary_contract.sql fails when a definer routine here is PUBLIC-callable.
do $$
declare
    routine text;
begin
    for routine in
        select p.oid::regprocedure::text
        from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'staging' and p.prosecdef
    loop
        execute format('revoke all on function %s from public', routine);
    end loop;
end $$;

-- #877 PR-4: mart.entity_identity + entity_display_resolution for consumer entity display and resolution
grant select on mart.entity_identity to mart_readonly;
grant select on mart.entity_identity to app_ops_reader;
grant select on mart.issuer_basic_info to mart_readonly, app_ops_reader;
grant select on mart.fund_holdings, mart.fund_holdings_resolved, mart.fund_holdings_coverage, mart.fund_holdings_valuation to mart_readonly;
grant select on mart.fund_holdings, mart.fund_holdings_resolved, mart.fund_holdings_coverage, mart.fund_holdings_valuation to app_ops_reader;
grant select on mart.entity_display_resolution to mart_readonly;
grant select on mart.entity_display_resolution to app_ops_reader;
-- The /admin/datahub dashboard (#641 D5) joins heads to their quality grades and
-- lists recent capture runs — read-only views/tables, same posture as the rest.
grant select on mart.datahub_quality_report to app_ops_reader;
-- #748: the weekly question-coverage report rendered on /admin/datahub.
grant select on mart.question_coverage_report to app_ops_reader;
-- The nightly confidence & accuracy report rendered on /admin/datahub.
grant select on mart.datahub_confidence_report to app_ops_reader;
grant select on mart.capture_status to app_ops_reader;
grant select on mart.capture_issuer_lineage to mart_readonly;
grant select on mart.capture_issuer_lineage to app_ops_reader;
grant select on mart.nightly_verdicts to app_ops_reader;
grant select on mart.oracle_outcomes to app_ops_reader;
grant select on mart.cell_confidence to app_ops_reader;
grant select on mart.capture_meta_info, mart.output_invariant_results to app_ops_reader;
grant select on mart.output_invariant_results to mart_readonly;
grant select on mart.fund_virtual_company to app_ops_reader;
grant select on mart.issuer_analyst_ratings to app_ops_reader;
grant select on mart.issuer_supply_chain_exposure to app_ops_reader;
grant select on mart.issuer_theme_purity to app_ops_reader;
grant select on mart.macro_series, mart.macro_series_daily, mart.macro_series_current, mart.macro_series_status to app_ops_reader;
grant select on mart.metric_node_values to app_ops_reader;
grant select on mart.research_card_artifacts to mart_readonly;
grant select on mart.research_card_artifacts to app_ops_reader;
-- The Capacity section (#671) derives capture-window durations from the
-- obligation results — read-only, same posture.
grant select on raw.capture_obligations to app_ops_reader;
grant select on raw.capture_obligation_results to app_ops_reader;
-- The N-PORT holdings plane (#63): filed weights become the first non-admin
-- read surface — read-only, same posture.
grant select on staging.fund_holding_facts to app_ops_reader;
grant app_ops_reader to app_service_login;
