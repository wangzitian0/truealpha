-- A Dagster step retry keeps the same run ID. Preserve each immutable source
-- attempt instead of colliding with a partial result written by an earlier
-- attempt or mutating that evidence in place.

alter table staging.capture_source_results
    add column if not exists attempt integer not null default 0;

do $$
declare
    constraint_name text;
begin
    select con.conname into constraint_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace ns on ns.oid = rel.relnamespace
    where ns.nspname = 'staging'
      and rel.relname = 'capture_source_results'
      and con.contype = 'u'
      and pg_get_constraintdef(con.oid) =
          'UNIQUE (run_id, subject_id, domain, partition_key, source)';

    if constraint_name is not null then
        execute format(
            'alter table staging.capture_source_results drop constraint %I',
            constraint_name
        );
    end if;
end $$;

create unique index if not exists uq_capture_source_result_attempt
    on staging.capture_source_results
       (run_id, subject_id, domain, partition_key, source, attempt);

alter table staging.capture_source_results
    drop constraint if exists capture_source_results_attempt_check;
alter table staging.capture_source_results
    add constraint capture_source_results_attempt_check check (attempt >= 0);
