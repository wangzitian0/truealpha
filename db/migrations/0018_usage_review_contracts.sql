-- Issue #58: persist bounded usage views and reverse quality reviews only
-- after the complete StrategyUsageAudit they reference has been stored.

-- #615: append, never rebuild.
--
-- This block used to drop the kind constraint and re-add it from a literal
-- list. 0038 and 0041 extend that same constraint ADDITIVELY — read what is
-- there, append one clause, no-op when it is already present.
-- `db/apply_migrations.sh` replays the WHOLE chain on every container boot, so
-- on a database that has reached the end of the chain, rebuilding from a
-- literal threw their clauses away and then validated the narrower list
-- against rows that need the wider one.
--
-- That is what took llm-service down on 2026-08-17: one `universe-list:qqq`
-- row, legal because 0041 allows it, made every boot abort here — 17 restart
-- loops on production, 182 on staging, and Traefik drops an unhealthy backend,
-- so /api/health, /health and /mcp all 404ed from app-web instead.
-- Reproduced on an isolated database: fresh chain passes, insert the row,
-- replay, abort. Fixed and verified the same way.
--
-- A first fix guarded this block with a plain "constraint exists -> return",
-- which is right for 0017 (the creator) and wrong here: a fresh database then
-- ended with 12 clauses instead of 14, silently dropping this migration's own
-- two kinds. Caught by counting the clauses rather than by the chain passing.
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
        raise exception 'contract_objects_kind_identity_check is missing; 0017 must apply first';
    end if;

    -- Idempotent: re-running must be a no-op, not a second pair of OR clauses.
    -- BOTH kinds are required before returning. Checking only the first would
    -- no-op on a partial state and leave the vocabulary incomplete (review) —
    -- and an incomplete vocabulary is what #615 was.
    if position('usage_frequency_slice' in existing_def) > 0
       and position('strategy_data_quality_review' in existing_def) > 0 then
        return;
    end if;

    execute 'alter table staging.contract_objects drop constraint contract_objects_kind_identity_check';
    execute format(
        'alter table staging.contract_objects add constraint contract_objects_kind_identity_check '
        'check ((%s) or (contract_kind = %L and contract_id like %L) '
        'or (contract_kind = %L and contract_id like %L))',
        regexp_replace(existing_def, '^CHECK\s*\((.*)\)$', '\1'),
        'usage_frequency_slice', 'usage-frequency:%',
        'strategy_data_quality_review', 'strategy-data-quality-review:%'
    );
end;
$$;

create index if not exists idx_contract_objects_usage_audit_run
    on staging.contract_objects ((payload ->> 'strategy_run_id'), recorded_at desc)
    where contract_kind = 'strategy_usage_audit';

create index if not exists idx_contract_objects_quality_review_run
    on staging.contract_objects ((payload ->> 'strategy_run_id'), recorded_at desc)
    where contract_kind = 'strategy_data_quality_review';
