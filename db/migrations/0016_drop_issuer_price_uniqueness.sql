-- PostgreSQL truncated the legacy constraint name created in 0004, so 0009's
-- name-based lookup did not remove it. Drop by semantic definition to allow
-- distinct share classes under one issuer to carry prices for the same date.
do $$
declare
    constraint_name text;
begin
    for constraint_name in
        select con.conname
        from pg_constraint con
        where con.conrelid = 'staging.market_prices'::regclass
          and con.contype = 'u'
          and pg_get_constraintdef(con.oid) =
              'UNIQUE (unified_id, trading_date, source, transaction_time)'
    loop
        execute format(
            'alter table staging.market_prices drop constraint %I',
            constraint_name
        );
    end loop;
end $$;

create unique index if not exists uq_market_prices_instrument_vintage
    on staging.market_prices (
        instrument_id, listing_id, trading_date, source, transaction_time,
        raw_ref, mapping_version
    );
