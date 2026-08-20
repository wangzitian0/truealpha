-- 0045: the deploy-verification canary (#648) is trigger-only — admit its job
-- name to the manual-trigger plane. The universe-list contract kind is already
-- generic (0041 registered 'universe-list:%'), so the canary head needs no
-- contract change; this is the whole migration.
alter table staging.pipeline_trigger_requests
    drop constraint if exists pipeline_trigger_requests_job_name_check;
alter table staging.pipeline_trigger_requests
    add constraint pipeline_trigger_requests_job_name_check
    check (job_name in ('topt_live_pipeline', 'qqq_live_pipeline', 'canary_live_pipeline'));
