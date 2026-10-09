-- #1176: the labor-efficiency metric name, per strategy decision.
--
-- The strategy evaluator ranks a FINANCIAL issuer on gppe_banking_tce_v1 and every other class on
-- gppe_uniform_charge_v0. The decision row carried the value only, so no reader could name the metric.
-- This column stores the name the evaluator used for that decision.
--
-- Nullable. A row written before this change holds NULL. So does an issuer with no metric (no class,
-- or excluded before a metric was chosen). A reader labels NULL as unrecorded. It never shows a bare number.
--
-- The name is an annotation, not part of the decision identity (content hash), the same as peg_reason_codes.
--
-- Replayed on every boot (#916). The statement adds the column only when the catalog lacks it.
-- A replay reads the catalog first and takes no lock on the table.

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.strategy_decisions'::regclass
          and attname = 'labor_efficiency_metric'
          and not attisdropped
    ) then
        alter table mart.strategy_decisions
            add column if not exists labor_efficiency_metric text;
    end if;
end
$$;
