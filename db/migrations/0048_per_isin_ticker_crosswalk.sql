-- #706: the valuation view resolved ticker through the ISSUER's newest ticker
-- vintage, which collapses share classes — the filing's GOOG line rendered as
-- GOOGL and joined GOOGL's core row. Enrichment now asserts each ISIN's OWN
-- ticker onto its minted per-ISIN entity (company:isin:<isin>); the view reads
-- that first and falls back to the issuer's newest ticker only while the
-- per-ISIN backfill has not covered a row yet.

create or replace view mart.fund_holdings_valuation as
with newest as (
    select distinct on (fund_id) fund_id, report_period, transaction_time
    from mart.fund_holdings
    order by fund_id, report_period desc, transaction_time desc
), resolved as (
    select holdings.*,
           (
               select identifier.entity_id
               from staging.kg_identifiers identifier
               where identifier.identifier_type = 'isin'
                 and identifier.identifier_value = holdings.isin
               order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
               limit 1
           ) as issuer_entity
    from mart.fund_holdings holdings
    join newest using (fund_id, report_period, transaction_time)
)
select resolved.fund_id,
       resolved.fund_name,
       resolved.report_period,
       resolved.transaction_time,
       resolved.holding_name,
       resolved.isin,
       resolved.percent_of_net_assets,
       resolved.value_usd,
       resolved.issuer_entity,
       coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value) as ticker,
       case
           when coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value) is not null
           then 'listing:xnas:' || lower(coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value))
       end as listing_id
from resolved
left join lateral (
    -- THIS ISIN's own listing (#706) — never another share class's.
    select identifier.identifier_value
    from staging.kg_identifiers identifier
    where identifier.identifier_type = 'ticker'
      and identifier.entity_id = 'company:isin:' || resolved.isin
    order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
    limit 1
) per_isin_ticker on true
left join lateral (
    select identifier.identifier_value
    from staging.kg_identifiers identifier
    where identifier.identifier_type = 'ticker'
      and identifier.entity_id = resolved.issuer_entity
      and resolved.issuer_entity like 'issuer:cik:%'
    order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
    limit 1
) issuer_ticker on true;

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.fund_holdings_valuation to mart_readonly;
    end if;
end;
$$;
