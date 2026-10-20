-- #1413 (Root 4, F2): Long-format metric node values table.
--
-- Decouples metric-forest node evaluation from wide-table column migrations:
-- every derived node evaluated during a run writes one row per
-- (run_id, issuer_id, node_id, period) into mart.metric_node_values.
-- Adding a new metric node or tree to the forest requires zero DDL changes.

create table if not exists mart.metric_node_values (
    run_id                    text not null,
    issuer_id                 text not null check (issuer_id <> ''),
    node_id                   uuid not null,
    node_key                  text not null check (node_key <> ''),
    period                    text not null check (period <> ''),
    period_tag                text,
    value                     numeric,
    unit                      text,
    availability_status       text not null
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error')),
    reason_codes              text[] not null default '{}',
    source_evidence_status    text not null
        check (source_evidence_status in ('verified', 'degraded', 'rejected')),
    factor_validation_status  text not null
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated')),
    confidence                numeric check (confidence is null or (confidence >= 0 and confidence <= 1)),
    confidence_band           text,
    tree_id                   uuid,
    tree_version              text,
    tree_sha256               text check (tree_sha256 is null or tree_sha256 ~ '^[0-9a-f]{64}$'),
    input_observation_ids     text[] not null default '{}',
    recorded_at               timestamptz not null default clock_timestamp(),
    primary key (run_id, issuer_id, node_id, period)
);

do $$
begin
    if to_regclass('mart.ix_metric_node_values_run_key') is null then
        create index if not exists ix_metric_node_values_run_key
            on mart.metric_node_values (run_id, node_key);
    end if;
    if to_regclass('mart.ix_metric_node_values_issuer_key') is null then
        create index if not exists ix_metric_node_values_issuer_key
            on mart.metric_node_values (issuer_id, node_key);
    end if;
end
$$;

do $$
begin
    if to_regprocedure('raw.reject_capture_control_mutation()') is not null
       and not exists (
           select 1
           from pg_trigger
           where tgname = 'reject_mutation'
             and tgrelid = 'mart.metric_node_values'::regclass
       )
    then
        create trigger reject_mutation
            before update or delete on mart.metric_node_values
            for each row execute function raw.reject_capture_control_mutation();
    end if;
end
$$;

comment on table mart.metric_node_values is
    '#1413 (docs/metric-forest.md §6): append-only long-format factor node outputs per (run_id, issuer_id, node_id, period).';

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly')
       and not has_table_privilege('mart_readonly', 'mart.metric_node_values', 'SELECT')
    then
        grant select on mart.metric_node_values to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader')
       and not has_table_privilege('app_ops_reader', 'mart.metric_node_values', 'SELECT')
    then
        grant select on mart.metric_node_values to app_ops_reader;
    end if;
end;
$$;
