-- Bitemporal + lineage completion for the two 0002-era tables that predate the
-- runtime contract (init.md Section 6, "Time axes" and "Source fusion").
--
-- Every staging table carries THREE time meanings:
--   valid_time        the real-world period the data describes
--   transaction_time  when the fact became PUBLICLY KNOWABLE (the DTO layer
--                     calls this knowable_at) — always set explicitly by the
--                     writer from a source property (filing date, publish
--                     time), NEVER defaulted to the insert clock: a new-source
--                     historical backfill stamped with now() would make five
--                     years of history invisible to every as-of query.
--   recorded_at       when TrueAlpha ingested the row — audit/reproducibility
--                     only, never read by as-of resolution.
--
-- 0004's tables (kg_identifiers, market_prices, analyst_rating_events,
-- fund_holding_facts) already have both axes; this migration brings
-- staging.financial_facts and staging.kg_edges up to the same contract and
-- completes financial_facts' lineage columns so the table matches the
-- FinancialFact DTO (libs/contracts) field for field — the DTO/DDL drift test
-- in apps/data-engine/tests locks the two together from here on.

-- ---------------------------------------------------------------------------
-- #1258: staging.financial_facts is retired.
-- 20261013T0700_datahub_retire_staging_financial_facts.sql drops it in deployed databases.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- staging.kg_edges (has data: bootstrap universe edges)
-- ---------------------------------------------------------------------------

do $$
begin
    if exists (
        select 1
        from pg_attrdef as default_row
        join pg_attribute as column_row
          on column_row.attrelid = default_row.adrelid
         and column_row.attnum = default_row.adnum
        where default_row.adrelid = 'staging.kg_edges'::regclass
          and column_row.attname = 'transaction_time'
    ) then
        alter table staging.kg_edges alter column transaction_time drop default;
    end if;
end
$$;

-- Backfill pre-existing rows with their transaction_time — the honest lower
-- bound (recorded no earlier than knowable, no later than this migration).
do $$
begin
    if not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.kg_edges'::regclass
          and attname = 'recorded_at' and not attisdropped
    ) then
        alter table staging.kg_edges add column if not exists recorded_at timestamptz;
    end if;
end
$$;
update staging.kg_edges set recorded_at = transaction_time where recorded_at is null;
do $$
begin
    if exists (
        select 1 from pg_attribute
        where attrelid = 'staging.kg_edges'::regclass
          and attname = 'recorded_at' and not attisdropped and not attnotnull
    ) then
        alter table staging.kg_edges alter column recorded_at set not null;
    end if;
end
$$;
do $$
begin
    if not exists (
        select 1
        from pg_attrdef as default_row
        join pg_attribute as column_row
          on column_row.attrelid = default_row.adrelid
         and column_row.attnum = default_row.adnum
        where default_row.adrelid = 'staging.kg_edges'::regclass
          and column_row.attname = 'recorded_at'
          and pg_get_expr(default_row.adbin, default_row.adrelid) = 'now()'
    ) then
        alter table staging.kg_edges alter column recorded_at set default now();
    end if;
end
$$;

-- entity_resolution.add_edge has always required raw_ref; enforce it. If this
-- fails, rows were written outside the library — that must surface, not hide.
do $$
begin
    if exists (
        select 1 from pg_attribute
        where attrelid = 'staging.kg_edges'::regclass
          and attname = 'raw_ref' and not attisdropped and not attnotnull
    ) then
        alter table staging.kg_edges alter column raw_ref set not null;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'kg_edges_recorded_after_knowable'
          and conrelid = 'staging.kg_edges'::regclass
    ) then
        alter table staging.kg_edges
            add constraint kg_edges_recorded_after_knowable
            check (recorded_at >= transaction_time);
    end if;
end $$;
