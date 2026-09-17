-- The N-PORT holdings read surface (#63 first tranche, B-phase reader step).
--
-- The normal-user App reads ONLY the mart schema through mart_readonly (#362);
-- staging.fund_holding_facts is therefore projected here. The view carries every
-- vintage — newest-per-fund selection is the reader's query, not baked in — and
-- joins the KG registry for display names. View runs with owner rights (the
-- migration role), which is the same mechanism mart.topt_capture_status uses to
-- read raw/staging on behalf of scoped-down readers.

do $$
declare
    wanted constant text := $view$
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
left join staging.kg_entities fund_entity on fund_entity.id = facts.fund_id
$view$;
begin
    -- `create or replace view` takes ACCESS EXCLUSIVE on the view even when nothing
    -- changes, queueing every reader behind it; replace only when the definition differs.
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.fund_holdings') is null
       or pg_get_viewdef(to_regclass('mart.fund_holdings'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.fund_holdings as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

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
