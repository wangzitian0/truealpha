-- #876 W1/W2: the verdict of every nightly in-environment check, one row per run.
--
-- The Dagster nightly checks (output invariants, the report surface proof, the confidence
-- report, the daily head reports, the model-provider key probe) run inside the environment's
-- own daemon; a red run was a row in dagster.runs and nothing else, and a daemon that stopped
-- ticking was indistinguishable from a quiet one. Each check now appends its verdict here —
-- green AND red — and the service's /health reports the newest row per check, so the scheduled
-- deploy-freshness workflow (tools/nightly_verdicts.py) can page on a red, a stale, or a missing
-- verdict from a runner that cannot reach this database.
--
-- Append-only: a re-run appends a new row; the newest (ran_at, recorded_at) per check is the
-- current verdict. `ran_at` is the run's own time (the schedule tick, or the wall clock at
-- completion for a manual run) — never this table's insertion clock, which is `recorded_at`.
-- `summary` is published on the public health endpoint, so writers put counts and names in it,
-- never research values, credentials, hosts or exception text.
create table if not exists mart.nightly_verdicts (
    verdict_id      bigint generated always as identity primary key,
    check_name      text not null check (check_name ~ '^[a-z0-9_]+(@[a-z0-9_.:-]+)?$'),
    ran_at          timestamptz not null,
    ok              boolean not null,
    summary         text not null check (char_length(summary) between 1 and 300),
    dagster_run_id  text not null check (char_length(dagster_run_id) between 1 and 64),
    recorded_at     timestamptz not null default clock_timestamp()
);

create index if not exists ix_nightly_verdicts_latest
    on mart.nightly_verdicts (check_name, ran_at desc, recorded_at desc);

comment on table mart.nightly_verdicts is
    '#876: one row per run of each nightly in-environment check (check_name[@universe]), green and red; ran_at is the tick (or completion time of a manual run); the newest row per check is reported on /health as nightly_verdicts and bounded by tools/nightly_verdicts.py.';

drop trigger if exists reject_mutation on mart.nightly_verdicts;
create trigger reject_mutation
before update or delete on mart.nightly_verdicts
for each row execute function mart.reject_mutation();

-- Read role: mart_readonly (the service's read-only account; the blanket grant in roles.sql
-- covers only tables that existed when the role was created). Conditional because CI applies
-- migrations before db/roles.sql creates the role.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.nightly_verdicts to mart_readonly;
    end if;
end;
$$;
