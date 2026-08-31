-- The N-PORT holdings read surface (#63 first tranche, B-phase reader step).
--
-- The normal-user App reads ONLY the mart schema through mart_readonly (#362);
-- staging.fund_holding_facts is therefore projected here. The view carries every
-- vintage — newest-per-fund selection is the reader's query, not baked in — and
-- joins the KG registry for display names. View runs with owner rights (the
-- migration role), which is the same mechanism mart.topt_capture_status uses to
-- read raw/staging on behalf of scoped-down readers.

create or replace view mart.fund_holdings as
select
    facts.fund_id,
    fund_entity.display_name as fund_name,
    facts.holding_id,
    facts.holding_name,
    facts.isin,
    facts.report_period,
    facts.transaction_time,
    facts.percent_of_net_assets,
    facts.value_usd,
    facts.balance,
    facts.confidence,
    facts.raw_ref
from staging.fund_holding_facts facts
left join staging.kg_entities fund_entity on fund_entity.id = facts.fund_id;

-- Explicit rather than relying on default privileges: the live databases get
-- migrations by hand (no tracking table), where the default-privilege owner is
-- not guaranteed to be the applying role. Conditional because CI applies
-- migrations BEFORE db/roles.sql creates the role (roles.sql's blanket
-- "all tables in schema mart" grant then covers the view there).
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.fund_holdings to mart_readonly;
    end if;
end;
$$;
