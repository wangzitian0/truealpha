-- Retire legacy wide mart tables and views (`mart.topt_gppe_*`, `mart.topt_core_*`),
-- backfilling any historical rows into `mart.metric_node_values` (#1730 Slice 4).
do $$
declare
    v_has_core boolean := to_regclass('mart.topt_core_results') is not null;
begin
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
