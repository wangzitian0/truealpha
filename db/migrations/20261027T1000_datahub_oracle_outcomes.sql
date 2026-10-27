-- #1523 S1: the record of every outside comparison.
--
-- A comparison of a served value with a witness value that this repository did not write is one
-- row of `mart.oracle_outcomes`. A later step writes the rows (S2) and the dashboard reads them
-- (S3). This step makes the table and keeps the one operator figure that a comparison needs.
--
-- `oracle_kind` names where the witness value comes from: `filing` (a figure the issuer filed),
-- `independent_parse` (a second parse of the same bytes), `operator` (the index operator's own
-- figure). `result` is `agree`, `disagree` or `not_comparable`. A `not_comparable` row says why in
-- `reason`. A compared row carries every number it was compared by: both values, the ratio
-- (served value divided by witness value), the tolerance, the time from which the witness value is
-- known (`knowable_at`) and the observation it was read from.
--
-- `compared_at` is the clock of the run that compared. The table has no insertion default for it,
-- so a replay of one run writes the same time. `recorded_at` is the audit time of the insert.
--
-- The rows are append-only. The key is (run, subject, field, period, oracle kind): a replay of one
-- comparison writes it once (`on conflict do nothing`).
--
-- Every statement here runs on every boot. A table or a column is made only when the catalog says
-- it is missing; a trigger, a comment and a grant are made only when they differ from what is
-- wanted. A settled database takes no lock.

create table if not exists mart.oracle_outcomes (
    outcome_id      bigint generated always as identity primary key,
    run_id          text not null check (run_id <> ''),
    subject_id      text not null check (subject_id <> ''),
    field           text not null check (field <> ''),
    period          text not null check (period <> ''),
    served_value    numeric check (served_value is null or served_value <> 'NaN'),
    oracle_value    numeric check (oracle_value is null or oracle_value <> 'NaN'),
    oracle_kind     text not null
        constraint oracle_outcomes_oracle_kind check (oracle_kind in ('filing', 'independent_parse', 'operator')),
    source          text not null check (source <> ''),
    observation_id  text check (observation_id is null or observation_id <> ''),
    knowable_at     timestamptz,
    ratio           numeric constraint oracle_outcomes_ratio check (ratio is null or (ratio >= 0 and ratio <> 'NaN')),
    tolerance       numeric
        constraint oracle_outcomes_tolerance check (tolerance is null or (tolerance >= 0 and tolerance <> 'NaN')),
    result          text not null
        constraint oracle_outcomes_result check (result in ('agree', 'disagree', 'not_comparable')),
    reason          text constraint oracle_outcomes_reason check (reason is null or reason <> ''),
    compared_at     timestamptz not null,
    recorded_at     timestamptz not null default clock_timestamp(),
    constraint oracle_outcomes_once unique (run_id, subject_id, field, period, oracle_kind),
    constraint oracle_outcomes_compared_carries_its_numbers check (
        result = 'not_comparable'
        or (
            served_value is not null
            and oracle_value is not null
            and ratio is not null
            and tolerance is not null
            and knowable_at is not null
            and observation_id is not null
        )
    ),
    constraint oracle_outcomes_not_comparable_says_why check (result <> 'not_comparable' or reason is not null)
);

do $$
begin
    if obj_description(to_regclass('mart.oracle_outcomes'), 'pg_class') is distinct from
       '#1523: one row per comparison of a served value with a witness value this repository did not write. Append-only. The key is (run_id, subject_id, field, period, oracle_kind). ratio is served_value divided by oracle_value. compared_at is the clock of the comparing run. knowable_at is the instant from which the witness value is known.' then
        comment on table mart.oracle_outcomes is
            '#1523: one row per comparison of a served value with a witness value this repository did not write. Append-only. The key is (run_id, subject_id, field, period, oracle_kind). ratio is served_value divided by oracle_value. compared_at is the clock of the comparing run. knowable_at is the instant from which the witness value is known.';
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = 'mart.oracle_outcomes'::regclass
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON mart.oracle_outcomes FOR EACH ROW EXECUTE FUNCTION mart.reject_mutation()'
    ) then
        drop trigger if exists reject_mutation on mart.oracle_outcomes;
        create trigger reject_mutation
        before update or delete on mart.oracle_outcomes
        for each row execute function mart.reject_mutation();
    end if;
end
$$;

-- Read roles. `mart_readonly` is the Web App and MCP account. `app_ops_reader` is the account of
-- `/admin/datahub`. The blanket grant in roles.sql covers only tables that existed when the role
-- was made, and CI applies migrations before roles.sql makes the roles. A role that has the grant
-- already is not granted again, so a replay takes no lock.
do $$
declare
    reader text;
begin
    foreach reader in array array['mart_readonly', 'app_ops_reader'] loop
        if exists (select from pg_roles where rolname = reader)
           and not has_table_privilege(reader, 'mart.oracle_outcomes', 'SELECT')
        then
            execute format('grant select on mart.oracle_outcomes to %I', reader);
        end if;
    end loop;
end
$$;

-- The operator quotes a last sale price with every market value. The pair is the only outside
-- witness of a listed unit count (market value divided by price). A row written before this
-- column exists keeps NULL: a comparison that needs the price reads it as `no_price`.
do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'staging.etf_constituent_facts'::regclass
          and attname = 'last_sale_price'
          and not attisdropped
    ) then
        alter table staging.etf_constituent_facts
            add column last_sale_price numeric check (last_sale_price is null or last_sale_price >= 0);
    end if;
end
$$;

do $$
begin
    if col_description('staging.etf_constituent_facts'::regclass,
                       (select attnum from pg_attribute
                        where attrelid = 'staging.etf_constituent_facts'::regclass and attname = 'last_sale_price'))
       is distinct from
       '#1523: the operator''s last sale price in USD, parsed from the same bytes as market_cap. NULL when the operator quoted none or the row predates the column.' then
        comment on column staging.etf_constituent_facts.last_sale_price is
            '#1523: the operator''s last sale price in USD, parsed from the same bytes as market_cap. NULL when the operator quoted none or the row predates the column.';
    end if;
end
$$;
