-- 20260922T0425_datahub_staging_market_prices.sql
-- Multi-resolution market prices staging tables and dynamic PIT universe mask (#101).

create table if not exists staging.market_prices_daily (
    symbol       text not null,
    date         date not null,
    open         numeric,
    high         numeric,
    low          numeric,
    close        numeric,
    volume       numeric,
    source       text not null default 'twelvedata',
    ingested_at  timestamptz not null default now(),
    primary key (symbol, date)
);

do $$
begin
    if to_regclass('staging.idx_market_prices_daily_date') is null then
        create index if not exists idx_market_prices_daily_date
            on staging.market_prices_daily (date, symbol);
    end if;
end
$$;

comment on table staging.market_prices_daily is
    '#101: Daily historical OHLCV bars ingested from Twelve Data or secondary origins.';

create table if not exists staging.market_prices_monthly (
    symbol       text not null,
    date         date not null, -- snapped to last XNYS session of month
    open         numeric,
    high         numeric,
    low          numeric,
    close        numeric,
    volume       numeric,
    source       text not null default 'twelvedata',
    resolution   text not null default '1M',
    ingested_at  timestamptz not null default now(),
    primary key (symbol, date)
);

do $$
begin
    if to_regclass('staging.idx_market_prices_monthly_date') is null then
        create index if not exists idx_market_prices_monthly_date
            on staging.market_prices_monthly (date, symbol);
    end if;
end
$$;

comment on table staging.market_prices_monthly is
    '#101: Monthly historical OHLCV bars snapped to the last XNYS session of each month.';

create table if not exists staging.universe_mask (
    symbol       text not null,
    cutoff_date  date not null,
    eligible     boolean not null,
    reason_code  text not null,
    computed_at  timestamptz not null default now(),
    primary key (symbol, cutoff_date)
);

do $$
begin
    if to_regclass('staging.idx_universe_mask_cutoff_eligible') is null then
        create index if not exists idx_universe_mask_cutoff_eligible
            on staging.universe_mask (cutoff_date, eligible);
    end if;
end
$$;

comment on table staging.universe_mask is
    '#101: Dynamic point-in-time (PIT) universe filter mask per cutoff date without lookahead.';
