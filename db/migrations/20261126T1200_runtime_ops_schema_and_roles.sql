-- Q04: Create additive `ops` schema views over operational/telemetry tables so
-- admin ops surfaces separate cleanly from analytical mart relations.
-- Every DDL statement is catalog-guarded so replaying under boot lock_timeout takes zero locks.

create schema if not exists ops;

do $$
begin
    if to_regclass('ops.nightly_verdicts') is null and to_regclass('mart.nightly_verdicts') is not null then
        execute 'create view ops.nightly_verdicts as select * from mart.nightly_verdicts';
    end if;

    if to_regclass('ops.output_invariant_results') is null and to_regclass('mart.output_invariant_results') is not null then
        execute 'create view ops.output_invariant_results as select * from mart.output_invariant_results';
    end if;

    if to_regclass('ops.pipeline_trigger_requests') is null and to_regclass('staging.pipeline_trigger_requests') is not null then
        execute $view$
            create view ops.pipeline_trigger_requests as
            select request_id, job_name, executed_at, requested_by, dedupe_key, requested_at, consumed_at, launched_run_key, config_json
            from staging.pipeline_trigger_requests
        $view$;
    end if;

    if to_regclass('ops.head_rollback_requests') is null and to_regclass('staging.head_rollback_requests') is not null then
        execute 'create view ops.head_rollback_requests as select * from staging.head_rollback_requests';
    end if;
end $$;
