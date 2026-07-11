-- A fund really can hold one issuer via TWO lines in one report period with an
-- identical holding name: MCHI's 2026-02-28 N-PORT lists Ping An / CATL / Midea /
-- BYD once as the A-share line and once as the H-share line, same name string,
-- different ISINs. The 0004 unique key (fund, name, period, transaction_time)
-- rejects the second line, so the line identifiers join the key. Kept as a
-- separate migration rather than editing 0004: applied databases exist.

do $$
declare
    conname text;
begin
    select c.conname into conname
    from pg_constraint c
    where c.conrelid = 'staging.fund_holding_facts'::regclass and c.contype = 'u';
    if conname is not null then
        execute format('alter table staging.fund_holding_facts drop constraint %I', conname);
    end if;
end;
$$;

create unique index if not exists uq_fund_holding_line_vintage
    on staging.fund_holding_facts (
        fund_id,
        holding_name,
        report_period,
        transaction_time,
        coalesce(isin, ''),
        coalesce(cusip, '')
    );
