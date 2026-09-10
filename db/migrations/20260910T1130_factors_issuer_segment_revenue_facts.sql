-- #772 (init.md §7 module 6): the segment-revenue fact plane, so q6 has a standard to
-- declare and the loop has somewhere to plan against.
--
-- Same PIT contract as staging.issuer_headcount_facts (#70): append-only, knowable_at is the
-- source's own time and never an insertion clock, corrections supersede by INSERT.
--
-- One difference shapes the whole table: a headcount is ONE number per issuer, while segment
-- revenue is MANY rows — one per reportable segment — and the rows are only meaningful as a
-- SET. A share computed over a set that missed a segment silently raises every remaining
-- segment's share and inverts the ranking q6 exists to produce, so the columns below carry
-- the set's own accounting identity rather than leaving it to whoever reads the rows:
--
--   partition_total     the consolidated revenue the parts were checked against
--   partition_residual  total - sum(parts), the honest size of the doubt
--   partition_id        the identity of the set this row belongs to
--
-- A reader can therefore refuse a partition it did not compute, which is the point:
-- `rule:exhaustive-partition:v1` accepted these rows together, and they are not admissible
-- apart.
create table if not exists staging.issuer_segment_revenue_facts (
    id                 bigint generated always as identity primary key,
    cik                integer not null check (cik > 0),
    -- The segment as the filing names it. Part of the row's identity — the metric registry
    -- holds one `segment_revenue`, not one metric per segment (init.md rule 22: no
    -- enumerated metric list outside the registry).
    segment_name       text not null check (segment_name <> ''),
    segment_revenue    numeric not null,
    -- Every row of one accepted partition shares this id. Content-addressed by the producer
    -- over (cik, period_end, the parts) so the same filing re-extracted lands the same id and
    -- a re-run collapses instead of duplicating a segment set.
    partition_id       text not null check (partition_id ~ '^segment-partition:[0-9a-f]{64}$'),
    -- The independent oracle the set was accepted against: the issuer's consolidated revenue,
    -- SEC-sourced, computed by a different path. Stored so a consumer can re-check rather
    -- than trust.
    partition_total    numeric not null,
    -- total - sum(parts). Zero for an exact partition; within the producer's tolerance
    -- otherwise. A row whose set was refused is never written, so this is always small — it
    -- says HOW small.
    partition_residual numeric not null,
    -- When the figure became knowable: the filing date. Never an insertion clock — a fact
    -- stamped at insert time is look-ahead for any historical cutoff.
    knowable_at        timestamptz not null,
    -- The fiscal period the segment revenue describes.
    period_end         date,
    source             text not null check (source <> ''),
    -- What justifies the number: accession + the span the value was read from. Required,
    -- because a segment revenue with no stated justification is indistinguishable from a
    -- guess — and unlike a headcount, a wrong one moves a RANKING.
    evidence_ref       text not null check (evidence_ref <> ''),
    -- The rule or model revision that CHOSE this set (`rule:exhaustive-partition:v1`, or a
    -- model identity). Kept per row so a partition's provenance survives a single-row read.
    extractor          text not null check (extractor <> ''),
    confidence         numeric not null check (confidence >= 0 and confidence <= 1),
    recorded_at        timestamptz not null default now(),
    check (recorded_at >= knowable_at - interval '400 days'),
    -- One issuer states a given segment once per period per partition. A restatement lands a
    -- NEW partition_id, so this never blocks a correction — it blocks the same set being
    -- written twice.
    unique (partition_id, segment_name)
);

-- `create table if not exists` does nothing when the table already exists, so a database
-- that got the table WITHOUT this constraint (an interrupted apply, a hand-created table,
-- or the replay of an earlier revision of this file) would keep accepting a segment stated
-- twice in one partition — the exact double-count the constraint exists to stop.
-- apply_migrations.sh replays every file on every boot, so the fix belongs here rather than
-- in a follow-up: the constraint is added if it is missing, whatever route the table took.
do $$
begin
    if not exists (
        select from pg_constraint
        where conrelid = 'staging.issuer_segment_revenue_facts'::regclass
          and contype = 'u'
          and conname = 'issuer_segment_revenue_facts_partition_id_segment_name_key'
    ) then
        alter table staging.issuer_segment_revenue_facts
            add constraint issuer_segment_revenue_facts_partition_id_segment_name_key
            unique (partition_id, segment_name);
    end if;
end;
$$;

create index if not exists ix_issuer_segment_revenue_facts_pit
    on staging.issuer_segment_revenue_facts (cik, knowable_at desc);

comment on table staging.issuer_segment_revenue_facts is
    '#772: append-only PIT segment revenue. Rows are admissible only as the partition they were accepted in — partition_total and partition_residual carry that set''s accounting identity so a consumer can refuse a set it did not compute.';
comment on column staging.issuer_segment_revenue_facts.partition_id is
    'Content-addressed id of the accepted set; every segment row of one extraction shares it. A restatement is a new partition, never an update.';
comment on column staging.issuer_segment_revenue_facts.partition_residual is
    'consolidated total minus the sum of the parts. The honest size of the doubt on any share computed from these rows.';

-- Reject mutation: this plane is append-only like every other PIT fact table.
drop trigger if exists reject_mutation on staging.issuer_segment_revenue_facts;
create trigger reject_mutation
before update or delete on staging.issuer_segment_revenue_facts
for each row execute function raw.reject_mutation();

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on staging.issuer_segment_revenue_facts to mart_readonly;
    end if;
end;
$$;
