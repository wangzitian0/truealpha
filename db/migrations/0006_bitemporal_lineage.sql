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
-- staging.financial_facts (empty everywhere: staging writers land after this)
-- ---------------------------------------------------------------------------

alter table staging.financial_facts alter column transaction_time drop default;

alter table staging.financial_facts add column if not exists recorded_at timestamptz;
update staging.financial_facts set recorded_at = transaction_time where recorded_at is null;
alter table staging.financial_facts alter column recorded_at set not null;
alter table staging.financial_facts alter column recorded_at set default now();

-- DTO parity: unit (XBRL units are NOT uniform across companies — a bare
-- numeric is not a fact), source_metric (the source's native tag, the mapping
-- layer's evidence), accession/form (SEC provenance, null for other sources).
alter table staging.financial_facts add column if not exists unit text;
update staging.financial_facts set unit = 'unknown' where unit is null;
alter table staging.financial_facts alter column unit set not null;

alter table staging.financial_facts add column if not exists source_metric text;
update staging.financial_facts set source_metric = metric where source_metric is null;
alter table staging.financial_facts alter column source_metric set not null;

alter table staging.financial_facts add column if not exists accession text;
alter table staging.financial_facts add column if not exists form text;

-- mapping_version stamps WHICH parser/mapping produced the row ("sec-companyfacts:1").
-- A reparse of the same raw bytes under a revised mapping is a new vintage that
-- must stay distinguishable from a source-side restatement (is_restatement) —
-- otherwise a backtest change can never be attributed to "data changed" vs
-- "cleaning logic changed".
alter table staging.financial_facts add column if not exists mapping_version text;
update staging.financial_facts set mapping_version = 'unversioned:0' where mapping_version is null;
alter table staging.financial_facts alter column mapping_version set not null;

-- A fact without lineage is not evidence.
alter table staging.financial_facts alter column raw_ref set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'financial_facts_recorded_after_knowable'
          and conrelid = 'staging.financial_facts'::regclass
    ) then
        alter table staging.financial_facts
            add constraint financial_facts_recorded_after_knowable
            check (recorded_at >= transaction_time);
    end if;
end $$;

-- Vintage identity now includes mapping_version (see above) and drops the
-- raw_ref coalesce (raw_ref is not null). Same index name as 0004 so replays
-- converge: 0004 recreates the old shape only on a fresh database, and this
-- block immediately upgrades it.
do $$
begin
    if exists (
        select 1 from pg_indexes
        where schemaname = 'staging'
          and indexname = 'uq_financial_facts_vintage'
          and indexdef not like '%mapping_version%'
    ) then
        drop index staging.uq_financial_facts_vintage;
    end if;
end $$;

create unique index if not exists uq_financial_facts_vintage
    on staging.financial_facts (
        unified_id,
        metric,
        fiscal_period,
        transaction_time,
        source,
        raw_ref,
        mapping_version
    );

-- ---------------------------------------------------------------------------
-- staging.kg_edges (has data: bootstrap universe edges)
-- ---------------------------------------------------------------------------

alter table staging.kg_edges alter column transaction_time drop default;

-- Backfill pre-existing rows with their transaction_time — the honest lower
-- bound (recorded no earlier than knowable, no later than this migration).
alter table staging.kg_edges add column if not exists recorded_at timestamptz;
update staging.kg_edges set recorded_at = transaction_time where recorded_at is null;
alter table staging.kg_edges alter column recorded_at set not null;
alter table staging.kg_edges alter column recorded_at set default now();

-- entity_resolution.add_edge has always required raw_ref; enforce it. If this
-- fails, rows were written outside the library — that must surface, not hide.
alter table staging.kg_edges alter column raw_ref set not null;

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
