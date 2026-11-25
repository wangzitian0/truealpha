-- Purge April-July 2026 capture chains pointing to non-existent buckets (#1815)
-- and replace fetches_object_uri_check with raw_fetches_bucket_check.
-- Split into ten DO blocks inside one transaction so each block completes well inside
-- apply_migrations.sh's per-statement timeout while keeping all temp tables atomic.
begin;

-- Block 1a: build temp tables and re-rooted current_pointer candidates.
do $$
declare
    added_count integer;
begin
    if exists (
        select 1
        from pg_constraint
        where conrelid = 'raw.fetches'::regclass
          and conname = 'raw_fetches_bucket_check'
          and convalidated
    ) then
        if exists (
            select 1
            from pg_constraint
            where conrelid = 'raw.fetches'::regclass
              and conname = 'fetches_object_uri_check'
        ) then
            alter table raw.fetches drop constraint fetches_object_uri_check;
        end if;
        return;
    end if;

    if not exists (select 1 from raw.fetches where object_uri !~ '^s3://truealpha-raw/.+' limit 1) then
        return;
    end if;

    perform set_config('work_mem', '128MB', true);

    create temp table _purge_disabled_triggers (tbl text primary key) on commit drop;

    create temp table _purge_bad_fetches on commit drop as
    select id, payload_sha256
      from raw.fetches
     where object_uri !~ '^s3://truealpha-raw/.+';
    alter table _purge_bad_fetches add primary key (id);
    create index on _purge_bad_fetches (payload_sha256);
    analyze _purge_bad_fetches;

    create temp table _purge_bad_vintages on commit drop as
    select distinct v.source_vintage_id, v.source_request_id
      from raw.capture_source_vintages v
      join _purge_bad_fetches bf on bf.id = v.raw_fetch_id;
    alter table _purge_bad_vintages add primary key (source_vintage_id);
    analyze _purge_bad_vintages;

    create temp table _purge_bad_attempts on commit drop as
    select distinct ar.attempt_id
      from raw.capture_attempt_results ar
      join _purge_bad_vintages bv on bv.source_vintage_id = ar.source_vintage_id
    union
    select distinct ar.attempt_id
      from raw.capture_attempt_results ar
      join _purge_bad_vintages bv on bv.source_vintage_id = ar.reused_source_vintage_id;
    alter table _purge_bad_attempts add primary key (attempt_id);
    analyze _purge_bad_attempts;

    create temp table _purge_bad_work_items on commit drop as
    select distinct a.work_item_id
      from raw.capture_attempts a
      join _purge_bad_attempts ba on ba.attempt_id = a.attempt_id;
    alter table _purge_bad_work_items add primary key (work_item_id);
    analyze _purge_bad_work_items;

    create temp table _purge_bad_runs on commit drop as
    select distinct o.run_id
      from raw.capture_obligations o
      join raw.capture_obligation_work_bindings b on b.obligation_id = o.obligation_id
      join _purge_bad_work_items bw on bw.work_item_id = b.work_item_id;
    alter table _purge_bad_runs add primary key (run_id);
    analyze _purge_bad_runs;

    create temp table _purge_bad_obligations on commit drop as
    select o.obligation_id
      from raw.capture_obligations o
      join _purge_bad_runs br on br.run_id = o.run_id
    union
    select b.obligation_id
      from raw.capture_obligation_work_bindings b
      join _purge_bad_work_items bw on bw.work_item_id = b.work_item_id;
    alter table _purge_bad_obligations add primary key (obligation_id);
    analyze _purge_bad_obligations;

    insert into _purge_bad_work_items (work_item_id)
    select distinct b.work_item_id
      from raw.capture_obligation_work_bindings b
      join _purge_bad_obligations bo on bo.obligation_id = b.obligation_id
     where not exists (
         select 1
           from raw.capture_obligation_work_bindings b2
          where b2.work_item_id = b.work_item_id
            and not exists (
                select 1 from _purge_bad_obligations bo2 where bo2.obligation_id = b2.obligation_id
            )
     )
    on conflict do nothing;
    analyze _purge_bad_work_items;

    insert into _purge_bad_attempts (attempt_id)
    select a.attempt_id
      from raw.capture_attempts a
      join _purge_bad_work_items bw on bw.work_item_id = a.work_item_id
    on conflict do nothing;
    analyze _purge_bad_attempts;

    create temp table _purge_candidate_requests on commit drop as
    select source_request_id from _purge_bad_vintages
    union
    select wi.source_request_id
      from raw.capture_work_items wi
      join _purge_bad_work_items bw on bw.work_item_id = wi.work_item_id;
    alter table _purge_candidate_requests add primary key (source_request_id);
    analyze _purge_candidate_requests;

    create temp table _purge_bad_obs on commit drop as
    select o.observation_id
      from staging.capture_normalized_observations o
      join _purge_bad_vintages bv on bv.source_vintage_id = o.source_vintage_id
    union
    select o.observation_id
      from staging.capture_normalized_observations o
      join _purge_bad_obligations bo on bo.obligation_id = o.capture_obligation_id;
    alter table _purge_bad_obs add primary key (observation_id);
    analyze _purge_bad_obs;

    -- Fixed-point closure for any cross-run observation reuse or supersedes chain.
    loop
        added_count := 0;

        insert into _purge_bad_obligations (obligation_id)
        select distinct coo.capture_obligation_id
          from staging.capture_observation_obligations coo
          join _purge_bad_obs bo on bo.observation_id = coo.observation_id
        on conflict do nothing;
        get diagnostics added_count = row_count;

        insert into _purge_bad_runs (run_id)
        select distinct o.run_id
          from raw.capture_obligations o
          join _purge_bad_obligations bo on bo.obligation_id = o.obligation_id
        on conflict do nothing;
        if found then
            added_count := added_count + 1;
        end if;

        insert into _purge_bad_obligations (obligation_id)
        select o.obligation_id
          from raw.capture_obligations o
          join _purge_bad_runs br on br.run_id = o.run_id
        on conflict do nothing;
        if found then
            added_count := added_count + 1;
        end if;

        insert into _purge_bad_obs (observation_id)
        select o.observation_id
          from staging.capture_normalized_observations o
          join _purge_bad_obligations bo on bo.obligation_id = o.capture_obligation_id
        union
        select o.observation_id
          from staging.capture_normalized_observations o
          join _purge_bad_obs bo on bo.observation_id = o.supersedes_observation_id
        on conflict do nothing;
        if found then
            added_count := added_count + 1;
        end if;

        exit when added_count = 0;
    end loop;
    analyze _purge_bad_runs;
    analyze _purge_bad_obligations;
    analyze _purge_bad_obs;

    create temp table _purge_bad_snapshots on commit drop as
    select cs.snapshot_id
      from staging.core_snapshots cs
      join _purge_bad_runs br on br.run_id = cs.run_id;
    alter table _purge_bad_snapshots add primary key (snapshot_id);
    analyze _purge_bad_snapshots;

    insert into _purge_bad_snapshots (snapshot_id)
    select distinct u.snapshot_id
      from (
          select csm.snapshot_id, unnest(csm.observation_ids) as observation_id
            from staging.core_snapshot_members csm
           where not exists (select 1 from _purge_bad_snapshots bs where bs.snapshot_id = csm.snapshot_id)
      ) u
      join _purge_bad_obs bo on bo.observation_id = u.observation_id
    on conflict do nothing;
    analyze _purge_bad_snapshots;

    create temp table _purge_bad_strategy_runs on commit drop as
    select distinct sb.strategy_run_id
      from mart.strategy_run_capture_bindings sb
      join _purge_bad_runs br on br.run_id = sb.capture_run_id
     where not exists (
         select 1
           from mart.strategy_run_capture_bindings sb2
          where sb2.strategy_run_id = sb.strategy_run_id
            and not exists (select 1 from _purge_bad_runs br2 where br2.run_id = sb2.capture_run_id)
     )
    union
    select sr.strategy_run_id
      from mart.strategy_runs sr
      join _purge_bad_snapshots bs on bs.snapshot_id = sr.snapshot_id
    union
    select sr.strategy_run_id
      from mart.strategy_runs sr
      join _purge_bad_fetches bf on sr.risk_free_fetch_id::text = bf.id::text;
    alter table _purge_bad_strategy_runs add primary key (strategy_run_id);
    analyze _purge_bad_strategy_runs;

    create temp table _purge_bad_nodes on commit drop as
    select n.node_id
      from staging.evidence_nodes n
      join _purge_bad_runs br on br.run_id = n.node_id
    union
    select n.node_id
      from staging.evidence_nodes n
      join _purge_bad_obs bo on bo.observation_id = n.node_id
    union
    select 'raw-fetch:' || bf.payload_sha256 as node_id
      from (select distinct payload_sha256 from _purge_bad_fetches) bf
      join staging.evidence_nodes n on n.node_id = 'raw-fetch:' || bf.payload_sha256
     where n.kind = 'raw_fetch'
       and not exists (
           select 1
             from raw.fetches gf
            where gf.payload_sha256 = bf.payload_sha256
              and gf.object_uri ~ '^s3://truealpha-raw/.+'
       );
    alter table _purge_bad_nodes add primary key (node_id);
    analyze _purge_bad_nodes;

    create temp table _purge_affected_cp_scopes on commit drop as
    select cp.environment, cp.universe_id, cp.universe_version, cp.factor_id
      from mart.current_pointer cp
      join _purge_bad_runs br on br.run_id = cp.target_run_id
    union
    select cp.environment, cp.universe_id, cp.universe_version, cp.factor_id
      from mart.current_pointer cp
      join _purge_bad_runs br on br.run_id = cp.previous_run_id;

    create temp table _purge_rebuilt_pointers on commit drop as
    with remaining as (
        select
            cp.environment, cp.universe_id, cp.universe_version, cp.factor_id,
            cp.target_run_id, cp.sequence, cp.advanced_at, cp.created_at,
            lag(cp.target_run_id) over (
                partition by cp.environment, cp.universe_id, cp.universe_version, cp.factor_id
                order by cp.sequence
            ) as prev_surviving_target
        from mart.current_pointer cp
        join _purge_affected_cp_scopes s
          on s.environment = cp.environment
         and s.universe_id = cp.universe_id
         and s.universe_version = cp.universe_version
         and s.factor_id = cp.factor_id
       where not exists (select 1 from _purge_bad_runs br where br.run_id = cp.target_run_id)
    ),
    deduped as (
        select environment, universe_id, universe_version, factor_id, target_run_id, sequence, advanced_at, created_at
          from remaining
         where prev_surviving_target is distinct from target_run_id
    ),
    resequenced as (
        select
            environment, universe_id, universe_version, factor_id, target_run_id,
            (row_number() over (partition by environment, universe_id, universe_version, factor_id order by sequence) - 1)::bigint as new_seq,
            lag(target_run_id) over (partition by environment, universe_id, universe_version, factor_id order by sequence) as new_prev,
            advanced_at, created_at
        from deduped
    ),
    hashed as (
        select
            environment, universe_id, universe_version, factor_id, target_run_id, new_seq, new_prev, advanced_at, created_at,
            raw.canonical_sha256(
                jsonb_build_object(
                    'advanced_at',
                    case
                        when (extract(microseconds from advanced_at at time zone 'UTC')::bigint % 1000000) = 0
                            then to_char(advanced_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                        else to_char(advanced_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                    end,
                    'key', jsonb_build_object('environment', environment, 'factor_id', factor_id, 'universe_id', universe_id, 'universe_version', universe_version),
                    'previous_run', case when new_prev is null then 'null'::jsonb else jsonb_build_object('kind', 'capture_run', 'node_id', new_prev) end,
                    'sequence', new_seq,
                    'target_run', jsonb_build_object('kind', 'capture_run', 'node_id', target_run_id)
                )
            ) as new_sha
        from resequenced
    )
    select 'current-pointer:' || new_sha as pointer_id, new_sha as content_sha256,
           environment, universe_id, universe_version, factor_id,
           target_run_id, new_seq as sequence, new_prev as previous_run_id, advanced_at, created_at
      from hashed;
end $$;

-- Block 1b: rebuild current_pointer and purge mart dependents.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_affected_cp_scopes limit 1) then
        alter table mart.current_pointer disable trigger user;
        insert into _purge_disabled_triggers values ('mart.current_pointer') on conflict do nothing;
        delete from mart.current_pointer cp
         using _purge_affected_cp_scopes s
         where cp.environment = s.environment
           and cp.universe_id = s.universe_id
           and cp.universe_version = s.universe_version
           and cp.factor_id = s.factor_id;
        insert into mart.current_pointer (
            pointer_id, content_sha256, environment, universe_id, universe_version,
            factor_id, target_run_id, sequence, previous_run_id, advanced_at, created_at
        )
        select pointer_id, content_sha256, environment, universe_id, universe_version,
               factor_id, target_run_id, sequence, previous_run_id, advanced_at, created_at
          from _purge_rebuilt_pointers
         order by environment, universe_id, universe_version, factor_id, sequence;
    end if;

    if exists (select 1 from _purge_bad_strategy_runs limit 1) then
        if exists (select 1 from mart.strategy_decisions sd join _purge_bad_strategy_runs bsr on bsr.strategy_run_id = sd.strategy_run_id limit 1) then
            alter table mart.strategy_decisions disable trigger user;
            insert into _purge_disabled_triggers values ('mart.strategy_decisions') on conflict do nothing;
            delete from mart.strategy_decisions sd using _purge_bad_strategy_runs bsr where sd.strategy_run_id = bsr.strategy_run_id;
        end if;
        if exists (select 1 from mart.strategy_run_capture_bindings sb join _purge_bad_strategy_runs bsr on bsr.strategy_run_id = sb.strategy_run_id limit 1) then
            alter table mart.strategy_run_capture_bindings disable trigger user;
            insert into _purge_disabled_triggers values ('mart.strategy_run_capture_bindings') on conflict do nothing;
            delete from mart.strategy_run_capture_bindings sb using _purge_bad_strategy_runs bsr where sb.strategy_run_id = bsr.strategy_run_id;
        end if;
    end if;
    if exists (select 1 from mart.strategy_run_capture_bindings sb join _purge_bad_runs br on br.run_id = sb.capture_run_id limit 1) then
        alter table mart.strategy_run_capture_bindings disable trigger user;
        insert into _purge_disabled_triggers values ('mart.strategy_run_capture_bindings') on conflict do nothing;
        delete from mart.strategy_run_capture_bindings sb using _purge_bad_runs br where sb.capture_run_id = br.run_id;
    end if;
    if exists (select 1 from _purge_bad_strategy_runs limit 1) then
        alter table mart.strategy_runs disable trigger user;
        insert into _purge_disabled_triggers values ('mart.strategy_runs') on conflict do nothing;
        delete from mart.strategy_runs sr using _purge_bad_strategy_runs bsr where sr.strategy_run_id = bsr.strategy_run_id;
    end if;
    if exists (select 1 from mart.cell_confidence cc join _purge_bad_runs br on br.run_id = cc.run_id limit 1)
       or exists (select 1 from mart.cell_confidence cc join _purge_bad_obs bo on bo.observation_id = cc.observation_id limit 1) then
        alter table mart.cell_confidence disable trigger user;
        insert into _purge_disabled_triggers values ('mart.cell_confidence') on conflict do nothing;
        delete from mart.cell_confidence cc using _purge_bad_runs br where cc.run_id = br.run_id;
        delete from mart.cell_confidence cc using _purge_bad_obs bo where cc.observation_id = bo.observation_id;
    end if;
    if exists (select 1 from mart.oracle_outcomes oo join _purge_bad_runs br on br.run_id = oo.run_id limit 1)
       or exists (select 1 from mart.oracle_outcomes oo join _purge_bad_obs bo on bo.observation_id = oo.observation_id limit 1) then
        alter table mart.oracle_outcomes disable trigger user;
        insert into _purge_disabled_triggers values ('mart.oracle_outcomes') on conflict do nothing;
        delete from mart.oracle_outcomes oo using _purge_bad_runs br where oo.run_id = br.run_id;
        delete from mart.oracle_outcomes oo using _purge_bad_obs bo where oo.observation_id = bo.observation_id;
    end if;
    if exists (select 1 from mart.datahub_confidence_report t join _purge_bad_runs br on br.run_id = t.run_id limit 1) then
        alter table mart.datahub_confidence_report disable trigger user;
        insert into _purge_disabled_triggers values ('mart.datahub_confidence_report') on conflict do nothing;
        delete from mart.datahub_confidence_report t using _purge_bad_runs br where t.run_id = br.run_id;
    end if;
    if exists (select 1 from mart.datahub_quality_report t join _purge_bad_runs br on br.run_id = t.run_id limit 1) then
        alter table mart.datahub_quality_report disable trigger user;
        insert into _purge_disabled_triggers values ('mart.datahub_quality_report') on conflict do nothing;
        delete from mart.datahub_quality_report t using _purge_bad_runs br where t.run_id = br.run_id;
    end if;
    delete from mart.fund_virtual_company t using _purge_bad_runs br where t.run_id = br.run_id;
    delete from mart.issuer_analyst_ratings t using _purge_bad_runs br where t.run_id = br.run_id;
    delete from mart.issuer_supply_chain_exposure t using _purge_bad_runs br where t.run_id = br.run_id;
    delete from mart.issuer_theme_purity t using _purge_bad_runs br where t.run_id = br.run_id;
    if exists (select 1 from mart.metric_node_values t join _purge_bad_runs br on br.run_id = t.run_id limit 1) then
        alter table mart.metric_node_values disable trigger user;
        insert into _purge_disabled_triggers values ('mart.metric_node_values') on conflict do nothing;
        delete from mart.metric_node_values t using _purge_bad_runs br where t.run_id = br.run_id;
    end if;
    delete from mart.question_coverage_report t using _purge_bad_runs br where t.run_id = br.run_id;
    if exists (select 1 from mart.research_card_artifacts t join _purge_bad_runs br on br.run_id = t.run_id limit 1) then
        alter table mart.research_card_artifacts disable trigger user;
        insert into _purge_disabled_triggers values ('mart.research_card_artifacts') on conflict do nothing;
        delete from mart.research_card_artifacts t using _purge_bad_runs br where t.run_id = br.run_id;
    end if;
    if exists (select 1 from mart.macro_series_daily m join _purge_bad_fetches bf on bf.id = m.fetch_id limit 1) then
        alter table mart.macro_series_daily disable trigger user;
        insert into _purge_disabled_triggers values ('mart.macro_series_daily') on conflict do nothing;
        delete from mart.macro_series_daily m using _purge_bad_fetches bf where m.fetch_id = bf.id;
    end if;
end $$;

-- Block 2a: purge staging evidence graph and snapshots.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_nodes limit 1) then
        alter table staging.evidence_edges disable trigger user;
        insert into _purge_disabled_triggers values ('staging.evidence_edges') on conflict do nothing;
        delete from staging.evidence_edges ee using _purge_bad_nodes bn where ee.from_id = bn.node_id;
        delete from staging.evidence_edges ee using _purge_bad_nodes bn where ee.to_id = bn.node_id;

        alter table staging.evidence_nodes disable trigger user;
        insert into _purge_disabled_triggers values ('staging.evidence_nodes') on conflict do nothing;
        update staging.evidence_nodes en set supersedes_node_id = null from _purge_bad_nodes bn where en.node_id = bn.node_id and en.supersedes_node_id is not null;
        update staging.evidence_nodes en set supersedes_node_id = null from _purge_bad_nodes bn where en.supersedes_node_id = bn.node_id;
        delete from staging.evidence_nodes en using _purge_bad_nodes bn where en.node_id = bn.node_id;
    end if;
    if exists (select 1 from _purge_bad_snapshots limit 1) then
        alter table staging.core_snapshot_members disable trigger user;
        insert into _purge_disabled_triggers values ('staging.core_snapshot_members') on conflict do nothing;
        delete from staging.core_snapshot_members csm using _purge_bad_snapshots bs where csm.snapshot_id = bs.snapshot_id;

        alter table staging.core_snapshots disable trigger user;
        insert into _purge_disabled_triggers values ('staging.core_snapshots') on conflict do nothing;
        delete from staging.core_snapshots cs using _purge_bad_snapshots bs where cs.snapshot_id = bs.snapshot_id;
    end if;
end $$;

-- Block 2b: purge staging normalized observations and raw-fetch staging refs.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_obs limit 1) then
        alter table staging.capture_observation_payloads disable trigger user;
        insert into _purge_disabled_triggers values ('staging.capture_observation_payloads') on conflict do nothing;
        delete from staging.capture_observation_payloads cop using _purge_bad_obs bo where cop.observation_id = bo.observation_id;
    end if;
    if exists (select 1 from _purge_bad_obs limit 1) or exists (select 1 from _purge_bad_obligations limit 1) then
        alter table staging.capture_observation_obligations disable trigger user;
        insert into _purge_disabled_triggers values ('staging.capture_observation_obligations') on conflict do nothing;
        delete from staging.capture_observation_obligations coo using _purge_bad_obs bo where coo.observation_id = bo.observation_id;
        delete from staging.capture_observation_obligations coo using _purge_bad_obligations bob where coo.capture_obligation_id = bob.obligation_id;
    end if;
    if exists (select 1 from _purge_bad_obs limit 1) then
        alter table staging.capture_normalized_observations disable trigger user;
        insert into _purge_disabled_triggers values ('staging.capture_normalized_observations') on conflict do nothing;
        update staging.capture_normalized_observations o set supersedes_observation_id = null from _purge_bad_obs bo where o.observation_id = bo.observation_id and o.supersedes_observation_id is not null;
        update staging.capture_normalized_observations o set supersedes_observation_id = null from _purge_bad_obs bo where o.supersedes_observation_id = bo.observation_id;
        delete from staging.capture_normalized_observations o using _purge_bad_obs bo where o.observation_id = bo.observation_id;
    end if;
    delete from staging.landed_fetch_keys lfk using _purge_bad_fetches bf where lfk.raw_fetch_id = bf.id;
    if exists (select 1 from staging.etf_constituent_facts ecf join _purge_bad_fetches bf on bf.id = ecf.raw_fetch_id limit 1) then
        alter table staging.etf_constituent_facts disable trigger user;
        insert into _purge_disabled_triggers values ('staging.etf_constituent_facts') on conflict do nothing;
        delete from staging.etf_constituent_facts ecf using _purge_bad_fetches bf where ecf.raw_fetch_id = bf.id;
    end if;
end $$;

-- Block 3a: purge raw control-plane results and work bindings.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_obligations limit 1) or exists (select 1 from _purge_bad_attempts limit 1) then
        alter table raw.capture_obligation_results disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_obligation_results') on conflict do nothing;
        delete from raw.capture_obligation_results cor using _purge_bad_obligations bo where cor.capture_obligation_id = bo.obligation_id;
        delete from raw.capture_obligation_results cor using _purge_bad_attempts ba where cor.final_attempt_id = ba.attempt_id;
    end if;
    if exists (select 1 from _purge_bad_attempts limit 1) or exists (select 1 from _purge_bad_vintages limit 1) then
        alter table raw.capture_attempt_results disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_attempt_results') on conflict do nothing;
        delete from raw.capture_attempt_results car using _purge_bad_attempts ba where car.attempt_id = ba.attempt_id;
        delete from raw.capture_attempt_results car using _purge_bad_vintages bv where car.source_vintage_id = bv.source_vintage_id;
        delete from raw.capture_attempt_results car using _purge_bad_vintages bv where car.reused_source_vintage_id = bv.source_vintage_id;
    end if;
    if exists (select 1 from _purge_bad_obligations limit 1) or exists (select 1 from _purge_bad_work_items limit 1) then
        alter table raw.capture_obligation_work_bindings disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_obligation_work_bindings') on conflict do nothing;
        delete from raw.capture_obligation_work_bindings cowb using _purge_bad_obligations bo where cowb.obligation_id = bo.obligation_id;
        delete from raw.capture_obligation_work_bindings cowb using _purge_bad_work_items bw where cowb.work_item_id = bw.work_item_id;
    end if;
end $$;

-- Block 3b: purge raw control-plane attempts and work items.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_attempts limit 1) then
        alter table raw.capture_attempts disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_attempts') on conflict do nothing;
        delete from raw.capture_attempts ca using _purge_bad_attempts ba where ca.attempt_id = ba.attempt_id;
    end if;
    if exists (select 1 from _purge_bad_work_items limit 1) then
        alter table raw.capture_work_items disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_work_items') on conflict do nothing;
        delete from raw.capture_work_items cwi using _purge_bad_work_items bw where cwi.work_item_id = bw.work_item_id;
    end if;
end $$;

-- Block 3c: purge raw control-plane plans, obligations, and runs.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_runs limit 1) then
        alter table raw.capture_run_plans disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_run_plans') on conflict do nothing;
        delete from raw.capture_run_plans crp using _purge_bad_runs br where crp.run_id = br.run_id;
    end if;
    if exists (select 1 from _purge_bad_obligations limit 1) then
        alter table raw.capture_obligations disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_obligations') on conflict do nothing;
        delete from raw.capture_obligations co using _purge_bad_obligations bo where co.obligation_id = bo.obligation_id;
    end if;
    if exists (select 1 from _purge_bad_runs limit 1) then
        alter table raw.capture_runs disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_runs') on conflict do nothing;
        delete from raw.capture_runs cr using _purge_bad_runs br where cr.run_id = br.run_id;
    end if;
end $$;

-- Block 4a: purge raw vintages and unreferenced candidate requests.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    if exists (select 1 from _purge_bad_vintages limit 1) then
        alter table raw.capture_source_vintages disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_source_vintages') on conflict do nothing;
        delete from raw.capture_source_vintages csv using _purge_bad_vintages bv where csv.source_vintage_id = bv.source_vintage_id;
    end if;
    if exists (select 1 from _purge_candidate_requests limit 1) then
        alter table raw.capture_source_requests disable trigger user;
        insert into _purge_disabled_triggers values ('raw.capture_source_requests') on conflict do nothing;
        delete from raw.capture_source_requests csr
         using _purge_candidate_requests crq
         where csr.source_request_id = crq.source_request_id
           and not exists (select 1 from raw.capture_source_vintages v where v.source_request_id = csr.source_request_id)
           and not exists (select 1 from raw.capture_work_items wi where wi.source_request_id = csr.source_request_id);
    end if;
end $$;

-- Block 4b: purge dangling-bucket raw.fetches rows.
do $$
begin
    if to_regclass('pg_temp._purge_bad_fetches') is null then
        return;
    end if;
    alter table raw.fetches disable trigger user;
    insert into _purge_disabled_triggers values ('raw.fetches') on conflict do nothing;
    delete from raw.fetches f using _purge_bad_fetches bf where f.id = bf.id;
end $$;

-- Block 4c: restore user triggers, drop old check, and enforce validated bucket check.
do $$
declare
    rec record;
begin
    if to_regclass('pg_temp._purge_disabled_triggers') is not null then
        for rec in select tbl from _purge_disabled_triggers order by tbl loop
            execute format('alter table %s enable trigger user', rec.tbl);
        end loop;
    end if;

    if exists (
        select 1
        from pg_constraint
        where conrelid = 'raw.fetches'::regclass
          and conname = 'fetches_object_uri_check'
    ) then
        alter table raw.fetches drop constraint fetches_object_uri_check;
    end if;

    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'raw.fetches'::regclass
          and conname = 'raw_fetches_bucket_check'
    ) then
        alter table raw.fetches
            add constraint raw_fetches_bucket_check check ((object_uri ~ '^s3://truealpha-raw/.+'::text)) not valid;
    end if;

    if exists (
        select 1
        from pg_constraint
        where conrelid = 'raw.fetches'::regclass
          and conname = 'raw_fetches_bucket_check'
          and not convalidated
    ) then
        alter table raw.fetches validate constraint raw_fetches_bucket_check;
    end if;
end $$;

commit;
