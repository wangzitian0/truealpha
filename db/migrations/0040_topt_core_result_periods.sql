-- 0040: the vintage axis reaches the mart (#530 slice 4).
--
-- FinancialFactPayload has carried operating/revenue/shares period ends since
-- #534, and the factor's staleness bound judges them — but the served mart rows
-- exposed only a boolean-ish freshness. "How old is the number this row serves"
-- was answerable only by re-deriving from the vendor (the V-2010 incident's
-- exact blind spot). Nullable: pre-#534 snapshots carry no periods.

alter table mart.topt_core_results
    add column if not exists operating_period_end date,
    add column if not exists revenue_period_end date,
    add column if not exists shares_period_end date;
