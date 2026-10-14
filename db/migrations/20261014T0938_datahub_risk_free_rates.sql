-- #1255: the risk-free rate of the core strategy is a measurement.
--
-- One row is one observation of the U.S. Treasury "Daily Treasury Par Yield Curve Rates",
-- column "3 Mo". The table replaces the two-point rate table inside the strategy definition.
-- `data_engine.datahub.risk_free` reads it. `data_engine.datahub.treasury_rates` writes it.
--
-- The rows are append-only. A revised value for one date is a new row. The reader takes the
-- newest row of the newest date that is knowable at the cutoff.
--
-- `annualized_rate` is a decimal fraction: 3.65 percent is 0.0365.
-- `observation_date` is the valid time. `knowable_at` is the transaction time. A rate is never
-- known on its own day, so `knowable_at` is 00:00 UTC of the next day. The CHECK holds that rule
-- in the table. `recorded_at` is the audit time of the insert and no reader orders by it first.
-- `fetch_id` points to the raw bytes in `raw.fetches` that carried the value.
--
-- Every statement here runs on every boot. Each one first asks the catalog whether it has work.

do $$
begin
    if to_regclass('staging.risk_free_rates_daily') is null then
        create table staging.risk_free_rates_daily (
            id                bigint generated always as identity primary key,
            instrument        text not null,
            observation_date  date not null,
            annualized_rate   numeric not null check (annualized_rate between 0 and 1),
            knowable_at       timestamptz not null,
            recorded_at       timestamptz not null default clock_timestamp(),
            fetch_id          bigint not null references raw.fetches (id),
            confidence        numeric not null check (confidence between 0 and 1),
            constraint risk_free_rates_daily_knowable_the_next_day
                check (knowable_at = ((observation_date + 1)::timestamp at time zone 'UTC'))
        );
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.idx_risk_free_rates_daily_asof') is null then
        create index if not exists idx_risk_free_rates_daily_asof
            on staging.risk_free_rates_daily (instrument, observation_date desc, recorded_at desc, id desc);
    end if;
end
$$;

do $$
begin
    if obj_description(to_regclass('staging.risk_free_rates_daily'), 'pg_class') is distinct from
       '#1255: append-only observations of the U.S. Treasury 3-month par yield, as decimal fractions. knowable_at is 00:00 UTC of the day after observation_date.' then
        comment on table staging.risk_free_rates_daily is
            '#1255: append-only observations of the U.S. Treasury 3-month par yield, as decimal fractions. knowable_at is 00:00 UTC of the day after observation_date.';
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = to_regclass('staging.risk_free_rates_daily')
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER trg_risk_free_rates_daily_append_only BEFORE DELETE OR UPDATE ON staging.risk_free_rates_daily FOR EACH ROW EXECUTE FUNCTION staging.reject_point_in_time_mutation()'
    ) then
        drop trigger if exists trg_risk_free_rates_daily_append_only on staging.risk_free_rates_daily;
        create trigger trg_risk_free_rates_daily_append_only
        before update or delete on staging.risk_free_rates_daily
        for each row execute function staging.reject_point_in_time_mutation();
    end if;
end
$$;
