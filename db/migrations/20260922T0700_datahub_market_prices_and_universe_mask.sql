-- #938 layer 1 rewrite: append-only, confidence-bearing OHLCV staging for the TOPT
-- backtest pipeline, and a PIT universe mask keyed by resolution.
--
-- Replaces the draft `staging.market_prices_daily` / `staging.market_prices_monthly` /
-- `staging.universe_mask` shape from the reverted #101 branch, which violated this
-- repository's PIT red lines: no `transaction_time`/`confidence`, and
-- `on conflict (...) do update` overwrote history in place. `staging.market_prices` (0004)
-- and `staging.mvp_market_prices` (0021) already own the general-purpose, KG-identity-keyed
-- price path; these two tables are the narrower TOPT-backtest-only OHLCV feed that
-- `data_engine.datahub.market_prices`/`lanes.market_data` write and a future
-- `BacktestDataGateway.price_bars()` reader turns into `PriceBar` rows -- `transaction_time`
-- here is exactly that reader's `PriceBar.knowable_at` (`libs/contracts/.../models.py`
-- `BacktestDataset.reject_lookahead`), and `recorded_at` is its `recorded_at`.
--
-- `transaction_time` is the XNYS session-close instant for the bar's own `trading_date`
-- (a calendar-derived source property, computed in `market_prices.xnys_session_close_utc`)
-- -- never an insertion-clock default, and never the instant THIS pipeline happened to
-- fetch or backfill the row (that is what `recorded_at` is for). No unique constraint
-- ties a row to its (symbol, trading_date): a re-ingested date is a new vintage row, and
-- the append-only trigger below rejects every UPDATE/DELETE outright so there is no
-- "insert or update" path to reach for.

do $$
begin
    if to_regclass('staging.market_prices_daily') is null then
        create table staging.market_prices_daily (
            id                bigint generated always as identity primary key,
            symbol            text not null,
            trading_date      date not null,
            open              numeric,
            high              numeric,
            low               numeric,
            close             numeric,
            volume            numeric,
            source            text not null default 'twelvedata',
            adjust            text not null,
            transaction_time  timestamptz not null,
            recorded_at       timestamptz not null default clock_timestamp(),
            confidence        numeric not null check (confidence between 0 and 1),
            raw_ref           text not null,
            check (recorded_at >= transaction_time)
        );
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.idx_market_prices_daily_symbol_date') is null then
        create index if not exists idx_market_prices_daily_symbol_date
            on staging.market_prices_daily (symbol, trading_date, transaction_time desc, recorded_at desc);
    end if;
end
$$;

comment on table staging.market_prices_daily is
    '#938: append-only daily OHLCV bars for the TOPT backtest universe (Twelve Data). '
    'transaction_time is the XNYS session close for trading_date; never updated in place.';

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = to_regclass('staging.market_prices_daily')
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_market_prices_daily_append_only BEFORE DELETE OR UPDATE ON staging.market_prices_daily FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation()'
    ) then
        drop trigger if exists trg_market_prices_daily_append_only on staging.market_prices_daily;
        create trigger trg_market_prices_daily_append_only
        before update or delete on staging.market_prices_daily
        for each row execute function staging.reject_point_in_time_mutation();
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.market_prices_monthly') is null then
        create table staging.market_prices_monthly (
            id                bigint generated always as identity primary key,
            symbol            text not null,
            trading_date      date not null, -- snapped to the last XNYS session of its month
            open              numeric,
            high              numeric,
            low               numeric,
            close             numeric,
            volume            numeric,
            source            text not null default 'twelvedata',
            adjust            text not null,
            transaction_time  timestamptz not null,
            recorded_at       timestamptz not null default clock_timestamp(),
            confidence        numeric not null check (confidence between 0 and 1),
            raw_ref           text not null,
            check (recorded_at >= transaction_time)
        );
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.idx_market_prices_monthly_symbol_date') is null then
        create index if not exists idx_market_prices_monthly_symbol_date
            on staging.market_prices_monthly (symbol, trading_date, transaction_time desc, recorded_at desc);
    end if;
end
$$;

comment on table staging.market_prices_monthly is
    '#938: append-only monthly OHLCV bars (last XNYS session of month) for the TOPT '
    'backtest universe (Twelve Data). Never updated in place.';

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = to_regclass('staging.market_prices_monthly')
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_market_prices_monthly_append_only BEFORE DELETE OR UPDATE ON staging.market_prices_monthly FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation()'
    ) then
        drop trigger if exists trg_market_prices_monthly_append_only on staging.market_prices_monthly;
        create trigger trg_market_prices_monthly_append_only
        before update or delete on staging.market_prices_monthly
        for each row execute function staging.reject_point_in_time_mutation();
    end if;
end
$$;

-- staging.universe_mask: a DERIVED eligibility judgment recomputed from the immutable
-- price tables above, not a raw ingested fact -- upserting it is safe (contract 1 flags
-- only the missing `resolution` column, never the upsert itself). The draft's PK was
-- (symbol, cutoff_date): the same as_of writes 1D then 1M and the second overwrites the
-- first's row outright. resolution joins the key so both resolutions keep their own row.
do $$
begin
    if to_regclass('staging.universe_mask') is null then
        create table staging.universe_mask (
            symbol       text not null,
            cutoff_date  date not null,
            resolution   text not null,
            eligible     boolean not null,
            reason_code  text not null,
            computed_at  timestamptz not null default clock_timestamp(),
            primary key (symbol, cutoff_date, resolution)
        );
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.idx_universe_mask_cutoff_eligible') is null then
        create index if not exists idx_universe_mask_cutoff_eligible
            on staging.universe_mask (resolution, cutoff_date, eligible);
    end if;
end
$$;

comment on table staging.universe_mask is
    '#938: dynamic point-in-time (PIT) universe filter mask per (symbol, cutoff_date, '
    'resolution) without lookahead. A missing row means ineligible (fail-closed).';
