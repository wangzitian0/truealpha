-- A new raw response may normalize to semantics already present (for example,
-- an overlapping Yahoo window). Reusing the semantic row while linking the new
-- raw vintage prevents duplicate facts without losing replay provenance.
create table if not exists staging.normalization_lineage (
    id                 bigint generated always as identity primary key,
    normalized_table   text not null,
    normalized_id      bigint not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    recorded_at        timestamptz not null default now(),
    unique (normalized_table, normalized_id, raw_ref, mapping_version)
);

create index if not exists idx_normalization_lineage_raw
    on staging.normalization_lineage (raw_ref, normalized_table);

drop trigger if exists trg_normalization_lineage_append_only on staging.normalization_lineage;
create trigger trg_normalization_lineage_append_only before update or delete on staging.normalization_lineage
for each row execute function staging.reject_point_in_time_mutation();
