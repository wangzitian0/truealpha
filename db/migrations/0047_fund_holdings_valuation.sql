-- The valuation face of the holdings plane (#63, B-phase reader step 2).
--
-- Joins each fund's NEWEST filed vintage to the KG crosswalk: ISIN -> newest
-- identifier vintage -> issuer entity -> newest ticker -> listing id. Runs with
-- owner rights over staging on the reader's behalf (same mechanism as 0046).
-- The valuation numbers themselves are NOT here: the reader joins this to
-- mart.topt_core_result_read at the governed pointer-head run — a join is a
-- read, not a computation (init.md principle 2; strategy-run-repository's
-- argument). Resolution uses the CURRENT newest vintage (no as_of): this is a
-- live read surface, not a point-in-time factor input.
--
-- listing id shape: 'listing:xnas:<ticker>' — every plane-published listing
-- today is XNAS (universe_plane.mic default). A multi-venue fund needs the
-- listing's own MIC carried through the crosswalk first; until then non-XNAS
-- rows simply fail the join and count as unvalued coverage, never as a guess.

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
       ticker.identifier_value as ticker,
       case
           when ticker.identifier_value is not null
           then 'listing:xnas:' || lower(ticker.identifier_value)
       end as listing_id
from resolved
left join lateral (
    select identifier.identifier_value
    from staging.kg_identifiers identifier
    where identifier.identifier_type = 'ticker'
      and identifier.entity_id = resolved.issuer_entity
      and resolved.issuer_entity like 'issuer:cik:%'
    order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
    limit 1
) ticker on true;

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.fund_holdings_valuation to mart_readonly;
    end if;
end;
$$;
