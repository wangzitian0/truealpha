-- Issues #58/#61: immutable capture promises and row-complete execution evidence.
-- A green scheduler state is not capture completeness; these tables persist the
-- exact pre-run scope and every subject/domain/partition result used by the gate.

create table if not exists staging.capture_scopes (
    capture_scope_id            text primary key,
    environment                 text not null,
    universe_id                 text not null,
    universe_version            text not null,
    universe_membership_sha256  text not null,
    as_of                       timestamptz not null,
    payload                     jsonb not null,
    recorded_at                 timestamptz not null default now(),
    check (capture_scope_id ~ '^capture-scope:[0-9a-f]{64}$'),
    check (environment in ('local', 'staging', 'production')),
    check (universe_membership_sha256 ~ '^[0-9a-f]{64}$')
);

create table if not exists staging.capture_manifests (
    capture_manifest_id  text primary key,
    capture_scope_id     text not null references staging.capture_scopes(capture_scope_id),
    run_id               text not null,
    image_digest         text not null,
    status               text not null,
    started_at           timestamptz not null,
    completed_at         timestamptz not null,
    blockers             jsonb not null,
    payload              jsonb not null,
    recorded_at          timestamptz not null default now(),
    check (capture_manifest_id ~ '^capture-manifest:[0-9a-f]{64}$'),
    check (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (status in ('pass', 'fail')),
    check (completed_at >= started_at),
    unique (capture_scope_id, run_id)
);

create table if not exists staging.capture_manifest_cells (
    capture_manifest_id    text not null references staging.capture_manifests(capture_manifest_id),
    subject_id             text not null,
    domain                 text not null,
    partition_key          text not null,
    requirement_level      text not null,
    status                 text not null,
    source                 text,
    record_count           integer not null,
    raw_refs               jsonb not null,
    normalized_record_ids  jsonb not null,
    content_sha256         text,
    min_knowable_at        timestamptz,
    max_knowable_at        timestamptz,
    source_recorded_at     timestamptz,
    observed_at            timestamptz,
    confidence             numeric,
    mapping_version        text,
    detail                 text,
    recorded_at            timestamptz not null default now(),
    primary key (capture_manifest_id, subject_id, domain, partition_key),
    check (requirement_level in ('required', 'optional', 'not_applicable')),
    check (status in ('complete', 'missing', 'unavailable', 'unresolved', 'stale', 'failed', 'not_applicable')),
    check (record_count >= 0),
    check (content_sha256 is null or content_sha256 ~ '^[0-9a-f]{64}$'),
    check (confidence is null or confidence between 0 and 1),
    check (source_recorded_at is null or max_knowable_at is null or source_recorded_at >= max_knowable_at)
);

create index if not exists idx_capture_manifests_scope_time
    on staging.capture_manifests (capture_scope_id, completed_at desc);

create index if not exists idx_capture_cells_status
    on staging.capture_manifest_cells (domain, status, subject_id);

drop trigger if exists trg_capture_scopes_append_only on staging.capture_scopes;
create trigger trg_capture_scopes_append_only before update or delete on staging.capture_scopes
for each row execute function raw.reject_mutation();

drop trigger if exists trg_capture_manifests_append_only on staging.capture_manifests;
create trigger trg_capture_manifests_append_only before update or delete on staging.capture_manifests
for each row execute function raw.reject_mutation();

drop trigger if exists trg_capture_manifest_cells_append_only on staging.capture_manifest_cells;
create trigger trg_capture_manifest_cells_append_only before update or delete on staging.capture_manifest_cells
for each row execute function raw.reject_mutation();
