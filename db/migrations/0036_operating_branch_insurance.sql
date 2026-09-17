-- #496: OperatingBranch gained INSURANCE (SIC 63xx, revenue minus policyholder
-- claims as the operating numerator — owner-approved 2026-07-28, mapping v3).
-- 0026's inline checks enumerated the branches; widen both. Found live: the
-- first mapping-v3 staging run failed on topt_gppe_results_operating_branch_check.
-- The factor path is uniform for insurers (numerator arrives in gross_profit,
-- metric stays capital_adjusted_gppe), so operating_metric's check is untouched.

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.topt_gppe_results'::regclass
          and conname = 'topt_gppe_results_operating_branch_check'
          and pg_get_constraintdef(oid) = 'CHECK ((operating_branch = ANY (ARRAY[''non_financial''::text, ''financial''::text, ''insurance''::text])))'
    ) then
        alter table mart.topt_gppe_results
            drop constraint if exists topt_gppe_results_operating_branch_check;
        alter table mart.topt_gppe_results
            add constraint topt_gppe_results_operating_branch_check
            check (operating_branch in ('non_financial', 'financial', 'insurance'));
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.topt_core_results'::regclass
          and conname = 'topt_core_results_operating_branch_check'
          and pg_get_constraintdef(oid) = 'CHECK ((operating_branch = ANY (ARRAY[''non_financial''::text, ''financial''::text, ''insurance''::text])))'
    ) then
        alter table mart.topt_core_results
            drop constraint if exists topt_core_results_operating_branch_check;
        alter table mart.topt_core_results
            add constraint topt_core_results_operating_branch_check
            check (operating_branch in ('non_financial', 'financial', 'insurance'));
    end if;
end
$$;
