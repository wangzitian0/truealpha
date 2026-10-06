-- Seal the legacy staging.market_prices table (0004) against new rows.
--
-- The 0004 market_prices table cannot be upgraded honestly: existing rows have
-- no reviewed confidence and include adjusted prices. Seal it instead of
-- inventing evidence.
--
-- #1061: this file also created six staging.mvp_* projection tables and their
-- trigger function. They are retired. 20261006T1325_datahub_retire_empty_planes.sql
-- drops them in deployed databases.

create or replace function staging.reject_legacy_market_price_insert()
returns trigger language plpgsql as $$
begin
    raise exception 'legacy staging.market_prices has no confidence contract and takes no new rows';
end;
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'staging.market_prices'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_market_prices_reject_insert BEFORE INSERT ON staging.market_prices FOR EACH ROW EXECUTE FUNCTION staging.reject_legacy_market_price_insert()'
    ) then
        drop trigger if exists trg_market_prices_reject_insert on staging.market_prices;
        create trigger trg_market_prices_reject_insert
        before insert on staging.market_prices
        for each row execute function staging.reject_legacy_market_price_insert();
    end if;
end
$$;
