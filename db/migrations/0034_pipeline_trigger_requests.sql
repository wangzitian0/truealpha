-- #495 (3/3): DB-mediated manual pipeline trigger. init.md §2.2 — the four
-- services exchange structured data ONLY through Postgres — so the admin UI
-- never calls Dagster over a network path it cannot (and must not) reach:
-- app-web INSERTs a typed request row here; the data-engine's Dagster sensor
-- (SensorDaemon, both environments) polls, launches `topt_live_pipeline` with
-- the requested `executed_at` (same thin-trigger, idempotent, content-
-- addressed semantics as the schedule, truealpha#491), and marks the row
-- consumed with the launched run's key.
--
-- Grants are the ONE sanctioned write exception for the app (issue #495
-- acceptance #4): app_runtime may INSERT (and read back status); data-engine
-- (table owner / migration role) UPDATEs consumption fields. No DELETE for
-- anyone — the request log is an audit trail.

create table if not exists staging.pipeline_trigger_requests (
    request_id      bigint generated always as identity primary key,
    job_name        text not null check (job_name = 'topt_live_pipeline'),
    executed_at     timestamptz not null,
    requested_by    text not null check (length(requested_by) > 0),
    dedupe_key      text not null unique,
    requested_at    timestamptz not null default clock_timestamp(),
    consumed_at     timestamptz,
    launched_run_key text,
    check ((consumed_at is null) = (launched_run_key is null))
);

comment on table staging.pipeline_trigger_requests is
    '#495: admin-initiated pipeline launches, mediated through Postgres (init.md 2.2). INSERT by app, consume-UPDATE by the Dagster sensor.';

-- app_runtime has no staging privileges at all today (db/roles.sql grants it
-- `usage` on schema app only). USAGE on staging + a grant on exactly this
-- table exposes nothing else: every other staging table stays unreadable
-- without its own table-level grant.
grant usage on schema staging to app_runtime;
grant select, insert on staging.pipeline_trigger_requests to app_runtime;

-- The app must not fabricate or rewrite consumption state, and nobody deletes
-- the audit trail. (UPDATE stays with the data-engine role that owns
-- migrations; a trigger enforces the append-only/consume-once shape.)
create or replace function staging.validate_pipeline_trigger_update()
returns trigger language plpgsql as $$
begin
    if old.consumed_at is not null then
        raise check_violation using message = 'pipeline trigger request already consumed';
    end if;
    if new.request_id <> old.request_id
       or new.job_name <> old.job_name
       or new.executed_at <> old.executed_at
       or new.requested_by <> old.requested_by
       or new.dedupe_key <> old.dedupe_key
       or new.requested_at <> old.requested_at then
        raise check_violation using message = 'pipeline trigger request fields are immutable';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_trigger_update on staging.pipeline_trigger_requests;
create trigger validate_trigger_update
before update on staging.pipeline_trigger_requests
for each row execute function staging.validate_pipeline_trigger_update();

create or replace function staging.reject_pipeline_trigger_delete()
returns trigger language plpgsql as $$
begin
    raise check_violation using message = 'pipeline trigger requests are an append-only audit trail';
end;
$$;

drop trigger if exists reject_trigger_delete on staging.pipeline_trigger_requests;
create trigger reject_trigger_delete
before delete on staging.pipeline_trigger_requests
for each row execute function staging.reject_pipeline_trigger_delete();
