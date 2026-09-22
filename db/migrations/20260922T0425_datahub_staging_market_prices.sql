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
    confidence   numeric not null default 1.0,
    raw_ref      text,
    ingested_at  timestamptz not null default now(),
    primary key (symbol, date)
);

do $$
begin
    if to_regclass('staging.market_prices_daily') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.market_prices_daily'::regclass and attname = 'confidence' and not attisdropped
    ) then
        alter table staging.market_prices_daily
            add column if not exists confidence numeric not null default 1.0;
    end if;
    if to_regclass('staging.market_prices_daily') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.market_prices_daily'::regclass and attname = 'raw_ref' and not attisdropped
    ) then
        alter table staging.market_prices_daily
            add column if not exists raw_ref text;
    end if;
end
$$;

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
    confidence   numeric not null default 1.0,
    raw_ref      text,
    ingested_at  timestamptz not null default now(),
    primary key (symbol, date)
);

do $$
begin
    if to_regclass('staging.market_prices_monthly') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.market_prices_monthly'::regclass and attname = 'confidence' and not attisdropped
    ) then
        alter table staging.market_prices_monthly
            add column if not exists confidence numeric not null default 1.0;
    end if;
    if to_regclass('staging.market_prices_monthly') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.market_prices_monthly'::regclass and attname = 'raw_ref' and not attisdropped
    ) then
        alter table staging.market_prices_monthly
            add column if not exists raw_ref text;
    end if;
end
$$;

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
    confidence   numeric not null default 1.0,
    raw_ref      text,
    computed_at  timestamptz not null default now(),
    primary key (symbol, cutoff_date)
);

do $$
begin
    if to_regclass('staging.universe_mask') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.universe_mask'::regclass and attname = 'confidence' and not attisdropped
    ) then
        alter table staging.universe_mask
            add column if not exists confidence numeric not null default 1.0;
    end if;
    if to_regclass('staging.universe_mask') is not null and not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.universe_mask'::regclass and attname = 'raw_ref' and not attisdropped
    ) then
        alter table staging.universe_mask
            add column if not exists raw_ref text;
    end if;
end
$$;

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
