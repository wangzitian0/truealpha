-- #874: a manual trigger can ask for a forced vendor fetch. The owner's standing
-- requirement is that a datahub tick runs both on schedule and by hand, and that a
-- hand-run can reach the vendors again. Without this, #635's 12-hour reuse window
-- made every same-day re-run bind the night's committed observations, so a new
-- origin or a corrected capture could not be proven until the next scheduled tick.
--
-- The request row carries the operator's choice. The Dagster sensor passes it to the
-- tick as `force_fetch` in the op config. Additive and defaulted: an app that never
-- names the column keeps inserting ordinary requests, and the sensor from before this
-- migration never reads it.

-- Boot-lock guard (2026-09-17): `add column if not exists` takes ACCESS EXCLUSIVE even when
-- the column is already there, so the replay on every boot only alters a table that lacks it.
do $$
begin
    if not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.pipeline_trigger_requests'::regclass and attname = 'force_fetch' and not attisdropped
    ) then
        alter table staging.pipeline_trigger_requests
            add column if not exists force_fetch boolean not null default false;
    end if;
end
$$;

comment on column staging.pipeline_trigger_requests.force_fetch is
    '#874: launch the tick with force_fetch — skip the #635 reuse window and fetch every obligation.';

-- 0034's immutability trigger, with the new field included: a request row is an audit
-- trail, and what was asked for cannot be rewritten before the sensor reads it. This
-- replaces 0034's body. Migrations re-apply in filename order, so this definition is
-- the one in force after every replay.
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
       or new.requested_at <> old.requested_at
       or new.force_fetch <> old.force_fetch then
        raise check_violation using message = 'pipeline trigger request fields are immutable';
    end if;
    return new;
end;
$$;
