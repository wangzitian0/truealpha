-- Retire universe-specific `production_topt` / `topt_*` naming on core capture tables,
-- views, constraints, and validator triggers (#1730 Slice 3).
do $$
declare
    v_con text;
    v_new text;
begin
    if to_regclass('raw.production_topt_run_plans') is null
       and to_regclass('staging.topt_core_snapshots') is null
       and to_regclass('staging.topt_core_snapshot_members') is null
       and to_regclass('mart.topt_capture_status') is null
       and to_regclass('mart.topt_capture_meta_info') is null
       and to_regprocedure('raw.validate_production_topt_run_plan()') is null
       and to_regprocedure('staging.validate_topt_core_snapshot()') is null
       and to_regprocedure('staging.validate_topt_core_snapshot_member()') is null
    then
        return;
    end if;

    -- When apply_migrations.sh runs 0001..0003 ahead of this migration on an upgraded DB,
    -- baseline DDL creates empty target tables alongside the legacy tables; drop those
    -- empty target tables (and swap/drop duplicate views after updating dependents) so
    -- we can rename the populated legacy relations in place without re-validating rows.
    if to_regclass('staging.topt_core_snapshot_members') is not null
       and to_regclass('staging.core_snapshot_members') is not null then
        if not exists (select 1 from staging.core_snapshot_members limit 1) then
            drop table staging.core_snapshot_members;
        end if;
    end if;

    if to_regclass('staging.topt_core_snapshots') is not null
       and to_regclass('staging.core_snapshots') is not null then
        if not exists (select 1 from staging.core_snapshots limit 1) then
            drop table staging.core_snapshots;
        end if;
    end if;

    if to_regclass('raw.production_topt_run_plans') is not null
       and to_regclass('raw.capture_run_plans') is not null then
        if not exists (select 1 from raw.capture_run_plans limit 1) then
            drop table raw.capture_run_plans;
        end if;
    end if;

    if to_regclass('raw.production_topt_run_plans') is not null
       and to_regclass('raw.capture_run_plans') is null then
        alter table raw.production_topt_run_plans rename to capture_run_plans;
    end if;

    if to_regclass('raw.capture_run_plans') is not null then
        for v_con, v_new in values
            ('production_topt_run_plans_pkey', 'capture_run_plans_pkey'),
            ('production_topt_run_plans_content_sha256_key', 'capture_run_plans_content_sha256_key'),
            ('production_topt_run_plans_content_sha256_check', 'capture_run_plans_content_sha256_check'),
            ('production_topt_run_plans_payload_check', 'capture_run_plans_payload_check'),
            ('production_topt_run_plans_release_manifest_id_check', 'capture_run_plans_release_manifest_id_check'),
            ('production_topt_run_plans_run_id_fkey', 'capture_run_plans_run_id_fkey')
        loop
            if exists (
                select 1 from pg_constraint
                where conrelid = 'raw.capture_run_plans'::regclass and conname = v_con
            ) and not exists (
                select 1 from pg_constraint
                where conrelid = 'raw.capture_run_plans'::regclass and conname = v_new
            ) then
                execute format(
                    'alter table raw.capture_run_plans rename constraint %I to %I',
                    v_con,
                    v_new
                );
            end if;
        end loop;
    end if;

    if to_regprocedure('raw.validate_production_topt_run_plan()') is not null then
        execute $fn$
CREATE OR REPLACE FUNCTION raw.validate_capture_run_plan()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if raw.canonical_sha256(new.payload) <> new.content_sha256
       or new.payload->>'run_id' <> new.run_id
       or new.payload->>'release_manifest_id' <> new.release_manifest_id then
        raise check_violation using message = 'Production TOPT run plan identity drifted';
    end if;
    return new;
end;
$function$;
        $fn$;
        drop trigger if exists validate_plan on raw.capture_run_plans;
        CREATE TRIGGER validate_plan BEFORE INSERT ON raw.capture_run_plans FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_run_plan();
        drop function if exists raw.validate_production_topt_run_plan();
    end if;

    if to_regclass('mart.data_engine_identity') is not null
       and pg_get_viewdef('mart.data_engine_identity'::regclass) ~* '\bproduction_topt_run_plans\b' then
        create or replace view mart.data_engine_identity as
         SELECT capture_run_plans.run_id,
            capture_run_plans.release_manifest_id,
            COALESCE(capture_run_plans.payload ->> 'data_engine_git_sha'::text, 'unknown'::text) AS git_sha,
            COALESCE(capture_run_plans.payload ->> 'data_engine_image_digest'::text, 'unknown'::text) AS image_digest,
            capture_run_plans.created_at
           FROM raw.capture_run_plans;
    end if;

    if to_regclass('mart.topt_capture_meta_info') is not null then
        if to_regclass('mart.capture_meta_info') is null then
            alter view mart.topt_capture_meta_info rename to capture_meta_info;
        else
            drop view mart.topt_capture_meta_info;
        end if;
    end if;

    if to_regclass('mart.topt_capture_status') is not null then
        if to_regclass('mart.capture_status') is null then
            alter view mart.topt_capture_status rename to capture_status;
        else
            drop view mart.topt_capture_status;
        end if;
    end if;

    if to_regclass('staging.topt_core_snapshots') is not null
       and to_regclass('staging.core_snapshots') is null then
        alter table staging.topt_core_snapshots rename to core_snapshots;
    end if;

    if to_regclass('staging.core_snapshots') is not null then
        for v_con, v_new in values
            ('topt_core_snapshots_pkey', 'core_snapshots_pkey'),
            ('topt_core_snapshots_content_sha256_key', 'core_snapshots_content_sha256_key'),
            ('topt_core_snapshots_run_id_key', 'core_snapshots_run_id_key'),
            ('topt_core_snapshots_check', 'core_snapshots_check'),
            ('topt_core_snapshots_content_sha256_check', 'core_snapshots_content_sha256_check'),
            ('topt_core_snapshots_instrument_count_check', 'core_snapshots_instrument_count_check'),
            ('topt_core_snapshots_issuer_count_check', 'core_snapshots_issuer_count_check'),
            ('topt_core_snapshots_observation_count_check', 'core_snapshots_observation_count_check'),
            ('topt_core_snapshots_payload_check', 'core_snapshots_payload_check'),
            ('topt_core_snapshots_snapshot_id_check', 'core_snapshots_snapshot_id_check'),
            ('topt_core_snapshots_universe_sha256_check', 'core_snapshots_universe_sha256_check'),
            ('topt_core_snapshots_run_id_fkey', 'core_snapshots_run_id_fkey')
        loop
            if exists (
                select 1 from pg_constraint
                where conrelid = 'staging.core_snapshots'::regclass and conname = v_con
            ) and not exists (
                select 1 from pg_constraint
                where conrelid = 'staging.core_snapshots'::regclass and conname = v_new
            ) then
                execute format(
                    'alter table staging.core_snapshots rename constraint %I to %I',
                    v_con,
                    v_new
                );
            end if;
        end loop;
    end if;

    if to_regclass('staging.topt_core_snapshot_members') is not null
       and to_regclass('staging.core_snapshot_members') is null then
        alter table staging.topt_core_snapshot_members rename to core_snapshot_members;
    end if;

    if to_regclass('staging.core_snapshot_members') is not null then
        for v_con, v_new in values
            ('topt_core_snapshot_members_pkey', 'core_snapshot_members_pkey'),
            ('topt_core_snapshot_members_snapshot_id_instrument_id_key', 'core_snapshot_members_snapshot_id_instrument_id_key'),
            ('topt_core_snapshot_members_snapshot_id_listing_id_key', 'core_snapshot_members_snapshot_id_listing_id_key'),
            ('topt_core_snapshot_members_factor_input_check', 'core_snapshot_members_factor_input_check'),
            ('topt_core_snapshot_members_member_sha256_check', 'core_snapshot_members_member_sha256_check'),
            ('topt_core_snapshot_members_observation_ids_check', 'core_snapshot_members_observation_ids_check'),
            ('topt_core_snapshot_members_snapshot_id_fkey', 'core_snapshot_members_snapshot_id_fkey')
        loop
            if exists (
                select 1 from pg_constraint
                where conrelid = 'staging.core_snapshot_members'::regclass and conname = v_con
            ) and not exists (
                select 1 from pg_constraint
                where conrelid = 'staging.core_snapshot_members'::regclass and conname = v_new
            ) then
                execute format(
                    'alter table staging.core_snapshot_members rename constraint %I to %I',
                    v_con,
                    v_new
                );
            end if;
        end loop;
    end if;

    if to_regprocedure('staging.validate_topt_core_snapshot()') is not null then
        execute $fn$
CREATE OR REPLACE FUNCTION staging.validate_core_snapshot()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    capture_status mart.capture_status%rowtype;
    release_exists boolean;
    release_matches_plan boolean;
begin
    select * into capture_status from mart.capture_status where run_id = new.run_id;
    if capture_status.run_id is null
       or capture_status.environment <> (select environment from mart.environment_identity)
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
        raise check_violation using message = 'core snapshot requires one complete exact capture run matching its own obligation count';
    end if;
    select exists (
        select 1 from staging.contract_objects
         where contract_id = new.release_manifest_id and contract_kind = 'release_manifest'
    ) into release_exists;
    select exists (
        select 1 from raw.capture_run_plans
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
$function$;
        $fn$;
        drop trigger if exists validate_snapshot on staging.core_snapshots;
        CREATE TRIGGER validate_snapshot BEFORE INSERT ON staging.core_snapshots FOR EACH ROW EXECUTE FUNCTION staging.validate_core_snapshot();
        drop function if exists staging.validate_topt_core_snapshot();
    end if;

    if to_regprocedure('staging.validate_topt_core_snapshot_member()') is not null then
        execute $fn$
CREATE OR REPLACE FUNCTION staging.validate_core_snapshot_member()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if new.observation_ids <> array(select unnest(new.observation_ids) order by 1)
       or cardinality(new.observation_ids) <> cardinality(array(select distinct unnest(new.observation_ids)))
       or raw.canonical_sha256(new.factor_input) <> new.member_sha256
       or new.factor_input->>'snapshot_id' <> new.snapshot_id
       or new.factor_input->>'instrument_id' <> new.instrument_id
       or new.factor_input->>'issuer_id' <> new.issuer_id
       or new.factor_input->>'listing_id' <> new.listing_id then
        raise check_violation using message = 'TOPT core snapshot member identity or payload drifted';
    end if;
    if exists (
        select 1 from unnest(new.observation_ids) observation_id
        left join staging.capture_normalized_observations observation using (observation_id)
        left join staging.capture_observation_payloads payload using (observation_id)
        where observation.observation_id is null or payload.observation_id is null
    ) then
        raise check_violation using message = 'TOPT core snapshot member lacks durable normalized payload lineage';
    end if;
    return new;
end;
$function$;
        $fn$;
        drop trigger if exists validate_member on staging.core_snapshot_members;
        CREATE TRIGGER validate_member BEFORE INSERT ON staging.core_snapshot_members FOR EACH ROW EXECUTE FUNCTION staging.validate_core_snapshot_member();
        drop function if exists staging.validate_topt_core_snapshot_member();
    end if;
end
$$;
