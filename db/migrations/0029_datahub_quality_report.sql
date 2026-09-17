-- Persisted row-complete DataHub quality report per capture run (#61 / #404).
-- One report per (run_id) over the exact requested-cell denominator; append-only.
create table if not exists mart.datahub_quality_report (
    report_id        text primary key,
    content_sha256   text not null,
    run_id           text not null,
    requested_count  integer not null,
    payload          jsonb not null,
    created_at       timestamptz not null default clock_timestamp(),
    constraint datahub_quality_report_id_shape
        check (report_id ~ '^datahub-quality-report:[0-9a-f]{64}$'),
    constraint datahub_quality_report_hash_matches_id
        check (split_part(report_id, ':', 2) = content_sha256)
);

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'mart.datahub_quality_report'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.datahub_quality_report FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation()'
    ) then
        drop trigger if exists reject_mutation on mart.datahub_quality_report;
        create trigger reject_mutation
        before update or delete on mart.datahub_quality_report
        for each row execute function mart.reject_mutation();
    end if;
end
$$;
