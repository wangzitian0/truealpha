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

alter table staging.contract_objects
    alter column recorded_at set default clock_timestamp();

-- The migration may already have run locally while Gate 0 is being expanded.
-- Replace any earlier anonymous kind check instead of leaving stale tiers unable
-- to persist the newly durable evidence objects.
do $$
declare
    existing_constraint record;
begin
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

create index if not exists idx_contract_objects_kind_recorded
    on staging.contract_objects (contract_kind, recorded_at desc);

drop trigger if exists trg_contract_objects_append_only on staging.contract_objects;
create trigger trg_contract_objects_append_only
before update or delete on staging.contract_objects
for each row execute function raw.reject_mutation();
