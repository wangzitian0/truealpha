-- #712: which data-engine build produced each run, as a mart projection consumers may read.
-- The run plan (raw.production_topt_run_plans.payload) carries data_engine_git_sha and
-- data_engine_image_digest since 2026-09-07; runs recorded before that read as 'unknown'.
-- A view, so it costs nothing per tick and needs no backfill; mart_readonly receives
-- select through db/roles.sql's default privileges on the mart schema.
create or replace view mart.data_engine_identity as
select run_id,
       release_manifest_id,
       coalesce(payload->>'data_engine_git_sha', 'unknown') as git_sha,
       coalesce(payload->>'data_engine_image_digest', 'unknown') as image_digest,
       created_at
from raw.production_topt_run_plans;
