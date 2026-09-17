-- Gate 0 (#56/#58): immutable content-addressed contract repository.
-- Typed application adapters revalidate payload semantics; the database keeps
-- the cross-kind identity and append-only guarantees enforceable on its own.

create table if not exists staging.contract_objects (
    contract_id       text primary key,
    contract_kind     text not null,
    content_sha256    text not null,
    payload           jsonb not null,
    recorded_at       timestamptz not null default clock_timestamp(),
    check (content_sha256 ~ '^[0-9a-f]{64}$'),
    check (contract_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'),
    check (split_part(contract_id, ':', 2) = content_sha256),
    check (jsonb_typeof(payload) = 'object')
);

-- Boot-lock guards (2026-09-17 incident): `db/apply_migrations.sh` replays this file on
-- every llm-service boot, and every statement below used to take its lock whether or not
-- there was anything to change. `alter column ... set default` is ACCESS EXCLUSIVE,
-- `create index if not exists` takes SHARE on the table BEFORE it looks for the name, and
-- `drop trigger` + `create trigger` is SHARE ROW EXCLUSIVE. A production backfill holding
-- ROW EXCLUSIVE on this table kept the v0.0.81 boot waiting 108 s here; the rollout failed
-- and the API was down until the backfill committed. Each statement now runs only when the
-- catalog says the object is not already in the state it declares, so a replay is a read.
do $$
begin
    if not exists (
        select 1
        from pg_attrdef as default_row
        join pg_attribute as column_row
          on column_row.attrelid = default_row.adrelid
         and column_row.attnum = default_row.adnum
        where default_row.adrelid = 'staging.contract_objects'::regclass
          and column_row.attname = 'recorded_at'
          and pg_get_expr(default_row.adbin, default_row.adrelid) = 'clock_timestamp()'
    ) then
        alter table staging.contract_objects
            alter column recorded_at set default clock_timestamp();
    end if;
end
$$;

-- The migration may already have run locally while Gate 0 is being expanded.
-- Replace any earlier anonymous kind check instead of leaving stale tiers unable
-- to persist the newly durable evidence objects.
do $$
declare
    existing_constraint record;
begin

    -- #615: own the creation, never the recreation.
    --
    -- This block used to drop the kind constraint and re-add it from the
    -- literal list below. 0038 and 0041 extend that same constraint
    -- ADDITIVELY — they read what exists, append one clause, and no-op if it
    -- is already there. `db/apply_migrations.sh` replays the WHOLE chain on
    -- every container boot, so on any database that has reached the end of the
    -- chain, this statement threw their clauses away and then validated the
    -- narrower list against rows that need the wider one.
    --
    -- That is what took llm-service down on 2026-08-17. One `universe-list:qqq`
    -- row — legal, 0041 allows it — made every boot abort here: 17 restart
    -- loops on production, 182 on staging, and the service was unreachable
    -- because Traefik drops an unhealthy backend. Reproduced on an isolated
    -- database: fresh chain passes, insert the row, replay, abort.
    --
    -- Widening the vocabulary belongs in a NEW migration that appends, which is
    -- what 0038 and 0041 already do correctly.
    if exists (
        select 1
        from pg_constraint as constraint_row
        join pg_class as table_row on table_row.oid = constraint_row.conrelid
        join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
        where schema_row.nspname = 'staging'
          and table_row.relname = 'contract_objects'
          and constraint_row.contype = 'c'
          and constraint_row.conname = 'contract_objects_kind_identity_check'
    ) then
        return;
    end if;
    alter table staging.contract_objects
        drop constraint if exists contract_objects_kind_identity_check;
    for existing_constraint in
        select constraint_row.conname
        from pg_constraint as constraint_row
        join pg_class as table_row on table_row.oid = constraint_row.conrelid
        join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
        where schema_row.nspname = 'staging'
          and table_row.relname = 'contract_objects'
          and constraint_row.contype = 'c'
          and pg_get_constraintdef(constraint_row.oid) like '%contract_kind%'
    loop
        execute format(
            'alter table staging.contract_objects drop constraint %I',
            existing_constraint.conname
        );
    end loop;

    alter table staging.contract_objects
    add constraint contract_objects_kind_identity_check check (
        (contract_kind = 'registry_snapshot' and contract_id like 'registry-snapshot:%')
        or (contract_kind = 'research_catalog_manifest' and contract_id like 'research-catalog:%')
        or (contract_kind = 'snapshot_manifest' and contract_id like 'snapshot:%')
        or (contract_kind = 'release_manifest' and contract_id like 'release-manifest:%')
        or (contract_kind = 'capture_scope' and contract_id like 'capture-scope:%')
        or (contract_kind = 'capture_manifest' and contract_id like 'capture-manifest:%')
        or (contract_kind = 'capture_evaluation_report' and contract_id like 'capture-evaluation:%')
        or (contract_kind = 'trace_bundle' and contract_id like 'trace-bundle:%')
        or (contract_kind = 'strategy_usage_audit' and contract_id like 'strategy-usage-audit:%')
        or (contract_kind = 'graduation_attestation' and contract_id like 'graduation-attestation:%')
    );
end;
$$;

do $$
begin
    if to_regclass('staging.idx_contract_objects_kind_recorded') is null then
        create index if not exists idx_contract_objects_kind_recorded
            on staging.contract_objects (contract_kind, recorded_at desc);
    end if;
end
$$;

-- The literal is `pg_get_triggerdef` of the statement below, so a replay that finds the
-- trigger exactly as declared touches nothing; an edited definition still converges.
-- Postgres prints the events in its own fixed order (INSERT, DELETE, UPDATE, TRUNCATE),
-- whatever order the statement lists them in. A literal that drifts from the statement
-- makes every replay take the lock, which test_migration_boot_locks.py fails on.
do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'staging.contract_objects'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_contract_objects_append_only BEFORE DELETE OR UPDATE ON staging.contract_objects FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation()'
    ) then
        drop trigger if exists trg_contract_objects_append_only on staging.contract_objects;
        create trigger trg_contract_objects_append_only
        before update or delete on staging.contract_objects
        for each row execute function raw.reject_mutation();
    end if;
end
$$;
