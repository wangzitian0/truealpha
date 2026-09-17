-- 0045: the deploy-verification canary (#648) is trigger-only — admit its job
-- name to the manual-trigger plane. The universe-list contract kind is already
-- generic (0041 registered 'universe-list:%'), so the canary head needs no
-- contract change; this is the whole migration.
--
-- Boot-lock guard (2026-09-17): the rebuild takes ACCESS EXCLUSIVE and re-validates every row,
-- so it runs only while the constraint is not already this one (the literal is its
-- pg_get_constraintdef; on a fresh database 0034's narrower inline check is still there).
do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'staging.pipeline_trigger_requests'::regclass
          and conname = 'pipeline_trigger_requests_job_name_check'
          and pg_get_constraintdef(oid) = 'CHECK ((job_name = ANY (ARRAY[''topt_live_pipeline''::text, ''qqq_live_pipeline''::text, ''canary_live_pipeline''::text])))'
    ) then
        alter table staging.pipeline_trigger_requests
            drop constraint if exists pipeline_trigger_requests_job_name_check;
        alter table staging.pipeline_trigger_requests
            add constraint pipeline_trigger_requests_job_name_check
            check (job_name in ('topt_live_pipeline', 'qqq_live_pipeline', 'canary_live_pipeline'));
    end if;
end
$$;
