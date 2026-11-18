-- Retire legacy wide mart tables and views (`mart.topt_gppe_*`, `mart.topt_core_*`),
-- backfilling any historical rows into `mart.metric_node_values` (#1730 Slice 4).
do $$
declare
    v_has_core boolean := to_regclass('mart.topt_core_results') is not null;
begin
    -- Re-create mart.capture_issuer_lineage if Slice 3 dropped an empty-table binding before renaming.
    if to_regclass('mart.capture_issuer_lineage') is null and to_regclass('staging.core_snapshot_members') is not null then
        execute $vc$create or replace view mart.capture_issuer_lineage as
 SELECT ('core-result:'::text || m.member_sha256) AS result_id,
    s.snapshot_id,
    s.run_id,
    s.release_manifest_id,
    s.universe_id,
    s.universe_version,
    s.universe_sha256,
    s.cutoff,
    m.issuer_id,
    m.instrument_id,
    m.listing_id,
    COALESCE(m.factor_input ->> 'operating_branch'::text, 'non_financial'::text) AS operating_branch,
    m.observation_ids AS input_observation_ids,
    mnv.tree_id,
    mnv.tree_version,
    mnv.tree_sha256 AS gppe_definition_sha256,
    mnv.confidence,
    periods.operating_period_end,
    periods.revenue_period_end,
    periods.shares_period_end,
    s.created_at,
    ( SELECT jsonb_agg(item.entry ORDER BY item.observation_id) AS items
           FROM ( SELECT DISTINCT ON (observation.observation_id) observation.observation_id,
                    jsonb_build_object('observation_id', observation.observation_id, 'semantic_type', observation.semantic_type, 'semantic_version', observation.semantic_version, 'source_vintage_id', observation.source_vintage_id, 'source_request_id', vintage.source_request_id, 'source_registry_entry_id', request.source_registry_entry_id, 'source_policy_id', request.source_policy_id, 'parser_version', observation.parser_version, 'mapping_version', observation.mapping_version, 'normalized_payload_sha256', observation.normalized_payload_sha256, 'confidence', observation.confidence, 'freshness',
                        CASE
                            WHEN (s.cutoff - observation.knowable_at) <= COALESCE(NULLIF(policy.semantic_freshness_max_age ->> observation.semantic_type, ''::text)::interval, policy.freshness_max_age) THEN 'fresh'::text
                            ELSE 'stale'::text
                        END, 'knowable_at', observation.knowable_at, 'recorded_at', observation.recorded_at, 'vintage', payload.normalized_payload -> 'vintage'::text, 'inputs', ( SELECT COALESCE(jsonb_object_agg(field.key, field.value), '{}'::jsonb) AS "coalesce"
                           FROM jsonb_each(payload.normalized_payload) field(key, value)
                          WHERE field.key = ANY (ARRAY['gross_profit'::text, 'total_assets'::text, 'headcount'::text, 'revenue'::text, 'shares_outstanding'::text, 'close'::text, 'currency'::text, 'listing_id'::text]))) AS entry
                   FROM unnest(m.observation_ids) selected(observation_id)
                     JOIN staging.capture_normalized_observations observation USING (observation_id)
                     JOIN staging.capture_observation_obligations usage USING (observation_id)
                     JOIN staging.capture_observation_payloads payload USING (observation_id)
                     JOIN raw.capture_obligations obligation ON obligation.obligation_id = usage.capture_obligation_id AND obligation.run_id = s.run_id
                     JOIN raw.capture_obligation_work_bindings binding ON binding.obligation_id = obligation.obligation_id
                     JOIN raw.capture_work_items work USING (work_item_id)
                     JOIN raw.capture_schedule_policies policy USING (schedule_policy_id)
                     JOIN raw.capture_source_vintages vintage USING (source_vintage_id)
                     JOIN raw.capture_source_requests request ON request.source_request_id = vintage.source_request_id
                  ORDER BY observation.observation_id, obligation.obligation_id) item) AS lineage
   FROM staging.core_snapshots s
     JOIN staging.core_snapshot_members m USING (snapshot_id)
     LEFT JOIN mart.metric_node_values mnv ON mnv.run_id = s.run_id AND mnv.issuer_id = m.issuer_id AND mnv.node_key = 'gppe_uniform_charge_v0'::text
     LEFT JOIN LATERAL ( SELECT (NULLIF(p.normalized_payload ->> 'operating_period_end'::text, ''::text))::date AS operating_period_end,
            (NULLIF(p.normalized_payload ->> 'revenue_period_end'::text, ''::text))::date AS revenue_period_end,
            (NULLIF(p.normalized_payload ->> 'shares_period_end'::text, ''::text))::date AS shares_period_end
           FROM unnest(m.observation_ids) sel(observation_id)
             JOIN staging.capture_normalized_observations o USING (observation_id)
             JOIN staging.capture_observation_payloads p USING (observation_id)
          WHERE o.semantic_type = 'financial-fact'::text
          ORDER BY o.knowable_at DESC, o.observation_id DESC
         LIMIT 1) periods ON true$vc$;
    end if;

    -- Ensure mart.metric_node_values permits 'not_applicable' on upgraded databases.
    if to_regclass('mart.metric_node_values') is not null then
        if exists (
            select 1
            from pg_constraint c
            where c.conrelid = 'mart.metric_node_values'::regclass
              and c.conname = 'metric_node_values_availability_status_check'
              and pg_get_constraintdef(c.oid) not like '%not_applicable%'
        ) then
            alter table mart.metric_node_values
                drop constraint metric_node_values_availability_status_check;
            alter table mart.metric_node_values
                add constraint metric_node_values_availability_status_check
                check (
                    availability_status = any (
                        array[
                            'available'::text,
                            'unavailable'::text,
                            'stale'::text,
                            'excluded'::text,
                            'low_confidence'::text,
                            'not_applicable'::text,
                            'error'::text
                        ]
                    )
                );
        end if;
    end if;

    if to_regclass('mart.topt_gppe_results') is not null then
        -- Preserve `not_applicable` on previously dual-written rows before dropping wide tables.
        if exists (
            select 1
            from mart.topt_gppe_results g
            join mart.metric_node_values mnv
              on mnv.run_id = g.run_id
             and mnv.issuer_id = g.issuer_id
             and mnv.node_key in ('gppe_uniform_charge_v0', 'capital_adjusted_gross_profit')
            where g.availability_status = 'not_applicable'
              and mnv.availability_status <> 'not_applicable'
            limit 1
        ) then
            alter table mart.metric_node_values disable trigger reject_mutation;
            update mart.metric_node_values mnv
            set availability_status = 'not_applicable'
            from mart.topt_gppe_results g
            where mnv.run_id = g.run_id
              and mnv.issuer_id = g.issuer_id
              and mnv.node_key in ('gppe_uniform_charge_v0', 'capital_adjusted_gross_profit')
              and g.availability_status = 'not_applicable'
              and mnv.availability_status <> 'not_applicable';
            alter table mart.metric_node_values enable trigger reject_mutation;
        end if;

        if v_has_core then
            execute $bf1$
                insert into mart.metric_node_values (
                    run_id, issuer_id, node_id, node_key, period, period_tag,
                    value, unit, availability_status, reason_codes,
                    source_evidence_status, factor_validation_status,
                    confidence, tree_id, tree_version, tree_sha256, input_observation_ids
                )
                select
                    g.run_id,
                    g.issuer_id,
                    '6143cf17-ceba-5f78-a2d3-a7cc423c49a7'::uuid,
                    'gppe_uniform_charge_v0',
                    coalesce(
                        to_char(max(c.operating_period_end), 'YYYY-MM-DD'),
                        to_char(g.cutoff at time zone 'UTC', 'YYYY-MM-DD')
                    ),
                    'FY',
                    g.gppe,
                    'per_employee',
                    coalesce(
                        g.availability_status,
                        case
                            when g.availability = 'unavailable' then 'unavailable'
                            when g.freshness = 'stale' then 'stale'
                            when g.freshness = 'unknown' then 'error'
                            when g.confidence < 0.50 then 'low_confidence'
                            else 'available'
                        end
                    ),
                    g.reason_codes,
                    coalesce(g.source_evidence_status, 'degraded'),
                    coalesce(g.factor_validation_status, 'not_evaluated'),
                    least(greatest(g.confidence, 0::numeric), 1::numeric),
                    'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid,
                    'production-topt-v0.2.0',
                    case
                        when g.gppe_definition_sha256 ~ '^[0-9a-f]{64}$' then g.gppe_definition_sha256
                        else null
                    end,
                    g.input_observation_ids
                from mart.topt_gppe_results g
                left join mart.topt_core_results c on c.gppe_result_id = g.result_id
                where g.issuer_id <> ''
                  and not exists (
                      select 1
                      from mart.metric_node_values existing
                      where existing.run_id = g.run_id
                        and existing.issuer_id = g.issuer_id
                        and existing.node_key = 'gppe_uniform_charge_v0'
                  )
                group by
                    g.run_id, g.issuer_id, g.cutoff, g.gppe, g.availability_status,
                    g.availability, g.freshness, g.confidence, g.reason_codes,
                    g.source_evidence_status, g.factor_validation_status,
                    g.gppe_definition_sha256, g.input_observation_ids
                on conflict (run_id, issuer_id, node_id, period) do nothing
            $bf1$;

            execute $bf2$
                insert into mart.metric_node_values (
                    run_id, issuer_id, node_id, node_key, period, period_tag,
                    value, unit, availability_status, reason_codes,
                    source_evidence_status, factor_validation_status,
                    confidence, tree_id, tree_version, tree_sha256, input_observation_ids
                )
                select
                    g.run_id,
                    g.issuer_id,
                    'c3428286-eb85-5107-a32a-b8757ad5b340'::uuid,
                    'capital_adjusted_gross_profit',
                    coalesce(
                        to_char(max(c.operating_period_end), 'YYYY-MM-DD'),
                        to_char(g.cutoff at time zone 'UTC', 'YYYY-MM-DD')
                    ),
                    'FY',
                    g.capital_adjusted_gross_profit,
                    'currency',
                    coalesce(
                        g.availability_status,
                        case
                            when g.availability = 'unavailable' then 'unavailable'
                            when g.freshness = 'stale' then 'stale'
                            when g.freshness = 'unknown' then 'error'
                            when g.confidence < 0.50 then 'low_confidence'
                            else 'available'
                        end
                    ),
                    g.reason_codes,
                    coalesce(g.source_evidence_status, 'degraded'),
                    coalesce(g.factor_validation_status, 'not_evaluated'),
                    least(greatest(g.confidence, 0::numeric), 1::numeric),
                    'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid,
                    'production-topt-v0.2.0',
                    case
                        when g.gppe_definition_sha256 ~ '^[0-9a-f]{64}$' then g.gppe_definition_sha256
                        else null
                    end,
                    g.input_observation_ids
                from mart.topt_gppe_results g
                left join mart.topt_core_results c on c.gppe_result_id = g.result_id
                where g.issuer_id <> ''
                  and not exists (
                      select 1
                      from mart.metric_node_values existing
                      where existing.run_id = g.run_id
                        and existing.issuer_id = g.issuer_id
                        and existing.node_key = 'capital_adjusted_gross_profit'
                  )
                group by
                    g.run_id, g.issuer_id, g.cutoff, g.capital_adjusted_gross_profit,
                    g.availability_status, g.availability, g.freshness, g.confidence,
                    g.reason_codes, g.source_evidence_status, g.factor_validation_status,
                    g.gppe_definition_sha256, g.input_observation_ids
                on conflict (run_id, issuer_id, node_id, period) do nothing
            $bf2$;
        else
            insert into mart.metric_node_values (
                run_id, issuer_id, node_id, node_key, period, period_tag,
                value, unit, availability_status, reason_codes,
                source_evidence_status, factor_validation_status,
                confidence, tree_id, tree_version, tree_sha256, input_observation_ids
            )
            select
                g.run_id,
                g.issuer_id,
                '6143cf17-ceba-5f78-a2d3-a7cc423c49a7'::uuid,
                'gppe_uniform_charge_v0',
                to_char(g.cutoff at time zone 'UTC', 'YYYY-MM-DD'),
                'FY',
                g.gppe,
                'per_employee',
                coalesce(
                    g.availability_status,
                    case
                        when g.availability = 'unavailable' then 'unavailable'
                        when g.freshness = 'stale' then 'stale'
                        when g.freshness = 'unknown' then 'error'
                        when g.confidence < 0.50 then 'low_confidence'
                        else 'available'
                    end
                ),
                g.reason_codes,
                coalesce(g.source_evidence_status, 'degraded'),
                coalesce(g.factor_validation_status, 'not_evaluated'),
                least(greatest(g.confidence, 0::numeric), 1::numeric),
                'ef13b940-b695-5f5f-846a-a36face5cc3e'::uuid,
                'production-topt-v0.2.0',
                case
                    when g.gppe_definition_sha256 ~ '^[0-9a-f]{64}$' then g.gppe_definition_sha256
                    else null
                end,
                g.input_observation_ids
            from mart.topt_gppe_results g
            where g.issuer_id <> ''
              and not exists (
                  select 1
                  from mart.metric_node_values existing
                  where existing.run_id = g.run_id
                    and existing.issuer_id = g.issuer_id
                    and existing.node_key = 'gppe_uniform_charge_v0'
              )
            on conflict (run_id, issuer_id, node_id, period) do nothing;
        end if;
    end if;

    drop view if exists mart.topt_core_result_read;
    drop view if exists mart.topt_core_meta_info;
    drop table if exists mart.topt_core_results;
    drop table if exists mart.topt_gppe_results;
    drop table if exists mart.topt_core_invocations;
    drop table if exists mart.topt_gppe_invocations;
    drop function if exists mart.validate_topt_core_result();
    drop function if exists mart.validate_topt_gppe_result();
end $$;
