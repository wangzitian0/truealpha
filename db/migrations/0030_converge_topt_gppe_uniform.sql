-- 0030: Converge production_topt GPPE onto the uniform base definition (#394).
--
-- production_topt now applies the same (numerator - total_assets*risk_free_rate)/headcount
-- capital-adjusted formula to every issuer (numerator = pre-provision profit for
-- financials, gross profit otherwise) and lets financial issuers flow through the
-- tier / P-S valuation path. The v0.1.0 financial short-circuit
-- (pre_provision_profit/headcount + financial_valuation_not_comparable) and its
-- `pre_provision_profit_per_employee` metric are retired, so the mart CHECK
-- constraints no longer special-case financial issuers.
--
-- Existing v0.1.0 rows remain (append-only prior vintage). New v0.2.0 rows use the
-- uniform shape below.

alter table mart.topt_gppe_results
    drop constraint topt_gppe_results_operating_metric_check,
    drop constraint topt_gppe_results_check1;

-- NOT VALID: enforce the uniform v0.2.0 shape on all NEW writes while grandfathering
-- any existing v0.1.0 rows (financial rows carrying the retired
-- pre_provision_profit_per_employee metric) as a prior append-only vintage. The mart
-- is append-only, so no UPDATE re-checks the old rows.
alter table mart.topt_gppe_results
    add constraint topt_gppe_results_operating_metric_check
        check (operating_metric = 'capital_adjusted_gppe') not valid,
    add constraint topt_gppe_results_uniform_values_check
        check (
            (availability = 'available' and cardinality(reason_codes) = 0
                and operating_efficiency is not null
                and operating_metric = 'capital_adjusted_gppe'
                and capital_adjusted_gross_profit is not null and gppe is not null)
            or
            (availability = 'unavailable' and cardinality(reason_codes) > 0
                and operating_efficiency is null and capital_adjusted_gross_profit is null
                and gppe is null)
        ) not valid;

alter table mart.topt_core_results
    drop constraint topt_core_results_operating_metric_check,
    drop constraint topt_core_results_check1;

alter table mart.topt_core_results
    add constraint topt_core_results_operating_metric_check
        check (operating_metric = 'capital_adjusted_gppe') not valid,
    add constraint topt_core_results_uniform_values_check
        check (
            (availability = 'available'
                and operating_metric = 'capital_adjusted_gppe' and operating_efficiency is not null
                and capital_adjusted_gross_profit is not null and gppe is not null
                and tier is not null and target_ps_lower is not null and target_ps_upper is not null
                and target_ps_midpoint is not null and current_ps is not null
                and valuation_gap is not null and cardinality(reason_codes) = 0)
            or
            (availability = 'unavailable' and capital_adjusted_gross_profit is null and gppe is null
                and tier is null and target_ps_lower is null and target_ps_upper is null
                and target_ps_midpoint is null and current_ps is null and valuation_gap is null
                and operating_efficiency is null and cardinality(reason_codes) > 0)
        ) not valid;
