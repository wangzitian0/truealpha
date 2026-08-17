-- 0042: generalize core-snapshot invariants beyond the TOPT 20/21/84 literals.
--
-- 0026 pinned staging.topt_core_snapshots to the founding TOPT universe with
-- absolute counts (issuer_count = 20, instrument_count = 21, observation_count
-- = 84) and a trigger that compared the capture run against a hardcoded 84.
-- The QQQ universe (102 listings -> 408 obligations) satisfies the same shape
-- but not the same numbers, so the constraints move to self-consistency:
-- counts must agree with each other and with the run's OWN obligation count,
-- exactly mirroring the Python-side generalization in materialization.py.

alter table staging.topt_core_snapshots
    drop constraint if exists topt_core_snapshots_issuer_count_check,
    drop constraint if exists topt_core_snapshots_instrument_count_check,
    drop constraint if exists topt_core_snapshots_observation_count_check;

alter table staging.topt_core_snapshots
    add constraint topt_core_snapshots_issuer_count_check
        check (issuer_count between 1 and instrument_count),
    add constraint topt_core_snapshots_instrument_count_check
        check (instrument_count >= 1),
    add constraint topt_core_snapshots_observation_count_check
        check (observation_count = 4 * instrument_count);

create or replace function staging.validate_topt_core_snapshot()
returns trigger language plpgsql as $$
declare
    capture_status mart.topt_capture_status%rowtype;
    release_exists boolean;
    release_matches_plan boolean;
begin
    select * into capture_status from mart.topt_capture_status where run_id = new.run_id;
    if capture_status.run_id is null
       or capture_status.environment <> 'production'
       or capture_status.obligation_count <> new.observation_count
       or capture_status.terminal_count <> capture_status.obligation_count
       or capture_status.success_count + capture_status.unchanged_count <> capture_status.obligation_count
       or capture_status.unavailable_count <> 0
       or capture_status.skipped_count <> 0
       or capture_status.failed_count <> 0
       or not capture_status.complete
       or capture_status.universe_id <> new.universe_id
       or capture_status.universe_version <> new.universe_version
       or capture_status.universe_sha256 <> new.universe_sha256
       or capture_status.cutoff <> new.cutoff then
        raise check_violation using message = 'core snapshot requires one complete exact Production capture run matching its own obligation count';
    end if;
    select exists (
        select 1 from staging.contract_objects
         where contract_id = new.release_manifest_id and contract_kind = 'release_manifest'
    ) into release_exists;
    select exists (
        select 1 from raw.production_topt_run_plans
         where run_id = new.run_id and release_manifest_id = new.release_manifest_id
    ) into release_matches_plan;
    if not release_exists or not release_matches_plan then
        raise check_violation using message = 'core snapshot release is not durable or does not match its run plan';
    end if;
    if raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'core snapshot payload hash does not match';
    end if;
    return new;
end;
$$;
