-- Freeze the deployment and scope before any scheduled source asset executes.
-- Retries reuse this row; a run may not silently change its release, image,
-- configuration, scope, or start boundary.

create table if not exists staging.capture_run_bindings (
    run_id                 text primary key,
    capture_scope_id       text not null references staging.capture_scopes(capture_scope_id),
    release_manifest_id    text not null,
    image_digest           text not null,
    configuration_sha256   text not null,
    schedule_name          text not null,
    started_at             timestamptz not null,
    recorded_at            timestamptz not null default now(),
    check (release_manifest_id ~ '^release-manifest:[0-9a-f]{64}$'),
    check (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (configuration_sha256 ~ '^[0-9a-f]{64}$'),
    check (recorded_at >= started_at)
);

create index if not exists idx_capture_run_bindings_scope
    on staging.capture_run_bindings (capture_scope_id, started_at desc);

drop trigger if exists trg_capture_run_bindings_append_only on staging.capture_run_bindings;
create trigger trg_capture_run_bindings_append_only before update or delete on staging.capture_run_bindings
for each row execute function staging.reject_point_in_time_mutation();
