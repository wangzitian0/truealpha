-- 0041: ETF/universe constituents become a data plane (#539 owner directive).
--
-- Universe membership was frozen corpus configuration compiled into the image;
-- adding an ETF meant writing code. Constituents are now captured like any other
-- source: raw bytes land content-addressed, parsed rows land here append-only
-- with lineage, and a governed list version is PUBLISHED from this plane
-- (contract_objects + staging.accepted_rulesets kind='universe-list:<etf>').
-- Index membership changes over time; this table is its PIT record.

create table if not exists staging.etf_constituent_facts (
    id            bigint generated always as identity primary key,
    etf_symbol    text not null check (etf_symbol <> ''),
    as_of         date not null,
    source        text not null check (source <> ''),
    raw_fetch_id  bigint not null references raw.fetches (id),
    ticker        text not null check (ticker <> ''),
    company_name  text not null default '',
    -- The official fund weight arrives with the N-PORT holdings plane (#63);
    -- NULL until then rather than a fabricated market-cap ratio. market_cap is
    -- the operator's own per-constituent figure, parsed from the same bytes.
    weight        numeric,
    market_cap    numeric,
    cik           integer,
    figi          text,
    knowable_at   timestamptz not null,
    recorded_at   timestamptz not null default clock_timestamp()
);

comment on table staging.etf_constituent_facts is
    'PIT ETF/index constituent rows with raw lineage; universe list versions are '
    'published from snapshots of this plane, never hand-edited (#539).';

create index if not exists etf_constituent_facts_lookup
    on staging.etf_constituent_facts (etf_symbol, as_of desc, ticker);

drop trigger if exists reject_mutation on staging.etf_constituent_facts;
create trigger reject_mutation
before update or delete on staging.etf_constituent_facts
for each row execute function raw.reject_mutation();

-- Register the universe-list contract kinds with the identity check, the 0038 way:
-- kind 'universe-list:<etf>' pairs with contract ids 'universe-list:<sha256>'.
do $$
declare
    existing_def text;
begin
    select pg_get_constraintdef(constraint_row.oid) into existing_def
    from pg_constraint as constraint_row
    join pg_class as table_row on table_row.oid = constraint_row.conrelid
    join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
    where schema_row.nspname = 'staging'
      and table_row.relname = 'contract_objects'
      and constraint_row.conname = 'contract_objects_kind_identity_check';

    if existing_def is null then
        raise exception 'contract_objects_kind_identity_check is missing; 0017/0018 must apply first';
    end if;

    -- Idempotent: re-running the migration file must be a no-op, not a second OR clause.
    if position('universe-list' in existing_def) > 0 then
        return;
    end if;

    execute 'alter table staging.contract_objects drop constraint contract_objects_kind_identity_check';
    execute format(
        'alter table staging.contract_objects add constraint contract_objects_kind_identity_check '
        'check ((%s) or (contract_kind like %L and contract_id like %L))',
        regexp_replace(existing_def, '^CHECK\s*\((.*)\)$', '\1'),
        'universe-list:%',
        'universe-list:%'
    );
end;
$$;

-- The operator UI reads this plane through app_ops_reader (traceability page).
-- Conditional: CI applies migrations BEFORE db/roles.sql, so on a fresh database
-- the role does not exist yet — roles.sql carries the permanent grants.
do $$
begin
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant usage on schema staging to app_ops_reader;
        grant select on staging.etf_constituent_facts to app_ops_reader;
        grant select on staging.accepted_rulesets to app_ops_reader;
        grant select on staging.accepted_ruleset_head to app_ops_reader;
        grant select on staging.contract_objects to app_ops_reader;
    end if;
end;
$$;

-- The thin manual trigger dispatches by job_name since the QQQ pipeline landed;
-- 0034's check pinned it to the single job of its era (Copilot High on #606 —
-- without this, QQQ trigger rows cannot even be inserted).
-- #615's shape again, on a second table, and the fix is to stop declaring the
-- vocabulary here at all.
--
-- This block dropped the constraint and re-added a two-value list while 0045
-- widens it to three. apply_migrations.sh replays the whole chain on every
-- container boot, so on any database that reached the end of the chain this
-- threw 0045's value away and then failed to validate against the canary rows
-- that need it.
--
-- Measured before fixing: staging carries 1 canary_live_pipeline row and
-- production 2. Staging's llm-service was crash-looping on exactly this
-- (restarts=5) after the v0.0.27 deploy; production had not restarted since
-- 2026-08-19 and would have failed on its next one.
--
-- Guarding the re-add with "constraint already exists -> return" was the first
-- attempt and is not enough: on a FRESH database this file still installs the
-- two-value list, and a reader of the chain still finds a narrower vocabulary
-- than the one that ends up in effect. 0045 is the authority for this
-- constraint and owns it alone.
--
-- The rows this migration's own feature needs (qqq_live_pipeline) are admitted
-- by 0045's list, so nothing here regresses.

