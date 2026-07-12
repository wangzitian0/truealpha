-- Issue #58: persist bounded usage views and reverse quality reviews only
-- after the complete StrategyUsageAudit they reference has been stored.

do $$
declare
    existing_constraint record;
begin
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
        or (contract_kind = 'usage_frequency_slice' and contract_id like 'usage-frequency:%')
        or (
            contract_kind = 'strategy_data_quality_review'
            and contract_id like 'strategy-data-quality-review:%'
        )
        or (contract_kind = 'graduation_attestation' and contract_id like 'graduation-attestation:%')
    );
end;
$$;

create index if not exists idx_contract_objects_usage_audit_run
    on staging.contract_objects ((payload ->> 'strategy_run_id'), recorded_at desc)
    where contract_kind = 'strategy_usage_audit';

create index if not exists idx_contract_objects_quality_review_run
    on staging.contract_objects ((payload ->> 'strategy_run_id'), recorded_at desc)
    where contract_kind = 'strategy_data_quality_review';
