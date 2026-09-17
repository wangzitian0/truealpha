-- 0040: the vintage axis reaches the mart (#530 slice 4).
--
-- FinancialFactPayload has carried operating/revenue/shares period ends since
-- #534, and the factor's staleness bound judges them — but the served mart rows
-- exposed only a boolean-ish freshness. "How old is the number this row serves"
-- was answerable only by re-deriving from the vendor (the V-2010 incident's
-- exact blind spot). Nullable: pre-#534 snapshots carry no periods.

-- Boot-lock guard (2026-09-17): `add column if not exists` takes ACCESS EXCLUSIVE even when
-- the column is already there, so the replay on every boot only alters a table that lacks it.
do $$
begin
    if exists (
        select 1 from unnest(array['operating_period_end', 'revenue_period_end', 'shares_period_end']) as wanted(column_name)
        where not exists (
            select 1 from pg_attribute
            where attrelid = 'mart.topt_core_results'::regclass and attname = wanted.column_name and not attisdropped
        )
    ) then
        alter table mart.topt_core_results
            add column if not exists operating_period_end date,
            add column if not exists revenue_period_end date,
            add column if not exists shares_period_end date;
    end if;
end
$$;
