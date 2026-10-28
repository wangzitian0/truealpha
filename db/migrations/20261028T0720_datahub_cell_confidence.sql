-- #1517: Append-only per-cell confidence table in mart and nullable staging observation confidence.

create table if not exists mart.cell_confidence (
    cell_confidence_id bigserial primary key,
    run_id text not null,
    subject_id text not null,
    semantic_type text not null,
    metric text not null,
    observation_id text,
    confidence numeric(10, 6) not null check (confidence >= 0 and confidence <= 1),
    policy_hash text not null,
    input_hash text not null,
    reason_codes text[] not null default '{}'::text[],
    recorded_at timestamptz not null default now(),
    constraint cell_confidence_run_subject_metric_unique unique (run_id, subject_id, metric)
);

create index if not exists idx_mart_cell_confidence_run_subject
    on mart.cell_confidence (run_id, subject_id);

create index if not exists idx_mart_cell_confidence_observation
    on mart.cell_confidence (observation_id)
    where observation_id is not null;

do $$
begin
    if (
        select d.description
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_description d on d.objoid = c.oid and d.objsubid = 0
        where n.nspname = 'mart' and c.relname = 'cell_confidence'
    ) is distinct from
       'Append-only per-cell continuous confidence record with policy hash, input hash, and reason codes (#1517).' then
        comment on table mart.cell_confidence is
            'Append-only per-cell continuous confidence record with policy hash, input hash, and reason codes (#1517).';
    end if;
end $$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger t
        join pg_class c on c.oid = t.tgrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart'
          and c.relname = 'cell_confidence'
          and t.tgname = 'trg_mart_cell_confidence_immutable'
    ) then
        create trigger trg_mart_cell_confidence_immutable
            before update or delete on mart.cell_confidence
            for each row execute function raw.reject_mutation();
    end if;
end $$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'mart_readonly')
       and not has_table_privilege('mart_readonly', 'mart.cell_confidence', 'SELECT') then
        grant select on mart.cell_confidence to mart_readonly;
    end if;
    if exists (select 1 from pg_roles where rolname = 'app_ops_reader')
       and not has_table_privilege('app_ops_reader', 'mart.cell_confidence', 'SELECT') then
        grant select on mart.cell_confidence to app_ops_reader;
    end if;
end $$;

do $$
begin
    if exists (
        select 1
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'staging'
          and c.relname = 'capture_normalized_observations'
          and a.attname = 'confidence'
          and a.attnotnull
          and not a.attisdropped
    ) then
        alter table staging.capture_normalized_observations
            alter column confidence drop not null;
    end if;
end $$;
