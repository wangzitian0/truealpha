-- #1255: macro base data. The first series are three U.S. Treasury par yields.
--
-- `mart.macro_series` is the catalog. The macro lane of the data engine writes it from the
-- source registration (`MACRO_REGISTRATIONS`) on every run. It holds the label, the unit and the
-- cadence of each series, so a reader never copies them.
--
-- `mart.macro_series_daily` holds one row per observation. The rows are append-only. A revised
-- value for one date is a new row. `observation_date` is the valid time. `knowable_at` is the
-- transaction time: the instant from which the source's publication makes the value known. It comes
-- from the source's own cut and never from the insertion clock. `recorded_at` is the audit time of the
-- insert and no reader orders by it first. `fetched_at` and `source` say when and from where the
-- bytes came. `fetch_id` points to the bytes in `raw.fetches`.
--
-- `value` is in the unit that the catalog names. For the Treasury yields the unit is an annualized
-- decimal fraction: 3.65 percent is 0.0365.
--
-- `mart.macro_series_current` keeps the newest vintage of each date.
-- `mart.macro_series_status` adds the newest date of each series and its age against the cadence.
-- The admin panel and the nightly verdict `macro_freshness` read the status view.
--
-- Every statement here runs on every boot. Each one first asks the catalog whether it has work.

create table if not exists mart.macro_series (
    series_id     text primary key check (series_id ~ '^[a-z0-9_]+$'),
    label         text not null check (label <> ''),
    unit          text not null check (unit <> ''),
    source_id     text not null check (source_id <> ''),
    max_age_days  integer not null check (max_age_days > 0),
    updated_at    timestamptz not null default clock_timestamp()
);

comment on table mart.macro_series is
    '#1255: catalog of macro series. Written by the macro lane from the source registration. max_age_days is the longest age in calendar days that the newest observation may have.';

create table if not exists mart.macro_series_daily (
    id                bigint generated always as identity primary key,
    series_id         text not null references mart.macro_series (series_id),
    observation_date  date not null,
    value             numeric not null check (value <> 'NaN'),
    knowable_at       timestamptz not null,
    source            text not null check (source <> ''),
    fetch_id          bigint not null references raw.fetches (id),
    fetched_at        timestamptz not null,
    confidence        numeric not null check (confidence between 0 and 1),
    recorded_at       timestamptz not null default clock_timestamp(),
    constraint macro_series_daily_knowable_after_the_day
        check (knowable_at >= ((observation_date + 1)::timestamp at time zone 'UTC'))
);

do $$
begin
    if to_regclass('mart.ix_macro_series_daily_asof') is null then
        create index if not exists ix_macro_series_daily_asof
            on mart.macro_series_daily (series_id, observation_date desc, recorded_at desc, id desc);
    end if;
end
$$;

do $$
begin
    if obj_description(to_regclass('mart.macro_series_daily'), 'pg_class') is distinct from
       '#1255: append-only observations of macro series. observation_date is the valid time. knowable_at is the transaction time from the source''s own cut. A revised value is a new row.' then
        comment on table mart.macro_series_daily is
            '#1255: append-only observations of macro series. observation_date is the valid time. knowable_at is the transaction time from the source''s own cut. A revised value is a new row.';
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'mart.macro_series_daily'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.macro_series_daily FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation()'
    ) then
        drop trigger if exists reject_mutation on mart.macro_series_daily;
        create trigger reject_mutation
        before update or delete on mart.macro_series_daily
        for each row execute function mart.reject_mutation();
    end if;
end
$$;

create or replace view mart.macro_series_current as
select distinct on (series_id, observation_date)
    series_id, observation_date, value, knowable_at, source, fetch_id, fetched_at, confidence, recorded_at
from mart.macro_series_daily
order by series_id, observation_date desc, recorded_at desc, id desc;

comment on view mart.macro_series_current is
    '#1255: the newest vintage of each observation date of each macro series.';

create or replace view mart.macro_series_status as
with newest as (
    select distinct on (series_id)
        series_id, observation_date, value, source, fetched_at
    from mart.macro_series_current
    order by series_id, observation_date desc
)
select
    s.series_id,
    s.label,
    s.unit,
    s.source_id,
    s.max_age_days,
    n.observation_date,
    n.value,
    n.source,
    n.fetched_at,
    (now() at time zone 'UTC')::date - n.observation_date as age_days,
    coalesce((now() at time zone 'UTC')::date - n.observation_date <= s.max_age_days, false) as is_fresh
from mart.macro_series s
left join newest n using (series_id);

comment on view mart.macro_series_status is
    '#1255: one row per macro series with its newest observation and its age in calendar days (UTC) against max_age_days. A series without an observation has a null date and is not fresh.';

-- Read role: mart_readonly. The blanket grant in roles.sql covers only tables that existed when
-- the role was created, and CI applies migrations before roles.sql creates the role.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.macro_series, mart.macro_series_daily,
            mart.macro_series_current, mart.macro_series_status to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.macro_series, mart.macro_series_daily,
            mart.macro_series_current, mart.macro_series_status to app_ops_reader;
    end if;
end
$$;

-- The decision lineage of the risk-free rate (#1255). A strategy run that evaluated a measured rate
-- names the series, the value, the observation date, the source and the `raw.fetches` row of it. A
-- decision joins to its run, so every decision can state which rate it used. A fixture or preview
-- replay uses the explicit rates of its corpus and leaves all five columns null.
do $$
begin
    if not exists (
        select 1 from pg_attribute
        where attrelid = 'mart.strategy_runs'::regclass
          and attname = 'risk_free_series_id' and not attisdropped
    ) then
        alter table mart.strategy_runs
            add column risk_free_series_id        text,
            add column risk_free_value            numeric,
            add column risk_free_observation_date date,
            add column risk_free_source           text,
            add column risk_free_fetch_id         bigint references raw.fetches (id);
        alter table mart.strategy_runs
            add constraint strategy_runs_risk_free_lineage_whole
            check (
                (risk_free_series_id is null and risk_free_value is null and risk_free_observation_date is null
                 and risk_free_source is null and risk_free_fetch_id is null)
                or
                (risk_free_series_id is not null and risk_free_value is not null
                 and risk_free_observation_date is not null and risk_free_source is not null
                 and risk_free_fetch_id is not null and risk_free_value between 0 and 1)
            );
    end if;
end
$$;

comment on column mart.strategy_runs.risk_free_value is
    '#1255: the annualized risk-free rate (decimal fraction) that the run evaluated, from the series named in risk_free_series_id on risk_free_observation_date; null for fixture and preview runs.';
