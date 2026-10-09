-- #1117: an issuer without a segment partition gets a Q6 row that says so.
--
-- Q6 wrote no row for a wide-row issuer that has no accepted segment partition.
-- The coverage report then showed `unavailable:no_row`. That text marks a join defect.
-- It does not name a reason. Q3 and Q4 already write an unvisited fill row (#1087).
-- Q6 now writes the same row, one per governed theme, with the reason `no_segment_partition`.
--
-- A fill row has no partition. So it has no cik, no period end, no partition id,
-- and no revenue mass. Eight columns of this table were NOT NULL, and the fill needs them empty.
-- This file makes those eight columns nullable.
--
-- The existing CHECK constraints stay as they are. A CHECK passes when its value is NULL.
-- So `cik > 0`, `consolidated_revenue > 0`, the partition id pattern, and the mass sum
-- all pass on a fill row. They still bind every row that has a partition, whatever its
-- availability status. A refused row (`unavailable` with a partition) keeps its sum check.
--
-- One new CHECK ties the eight columns to the fill marker, so a NULL cannot enter by mistake:
--   * A fill row (extractor `lane:unvisited:v1`) holds NULL in all eight columns.
--     It is `unavailable` and it holds no share.
--   * Every other row holds a value in all eight columns.
--
-- Replayed on every boot (#916). Each statement runs only when the catalog says it must.
-- ALTER TABLE takes ACCESS EXCLUSIVE even when it changes nothing. So a replay reads the catalog first
-- and takes no lock on the table. The first apply changes the catalog only.

do $$
begin
    if exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.issuer_theme_purity'::regclass
          and attname in (
              'cik', 'period_end', 'partition_id', 'consolidated_revenue',
              'in_theme_revenue', 'out_of_theme_revenue', 'unclassified_revenue', 'partition_residual'
          )
          and attnotnull
          and not attisdropped
    ) then
        alter table mart.issuer_theme_purity
            alter column cik drop not null,
            alter column period_end drop not null,
            alter column partition_id drop not null,
            alter column consolidated_revenue drop not null,
            alter column in_theme_revenue drop not null,
            alter column out_of_theme_revenue drop not null,
            alter column unclassified_revenue drop not null,
            alter column partition_residual drop not null;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'mart.issuer_theme_purity'::regclass
          and conname = 'issuer_theme_purity_partition_or_fill_check'
    ) then
        alter table mart.issuer_theme_purity
            add constraint issuer_theme_purity_partition_or_fill_check check (
                (
                    extractor = 'lane:unvisited:v1'
                    and availability_status = 'unavailable'
                    and theme_share is null
                    and cik is null
                    and period_end is null
                    and partition_id is null
                    and consolidated_revenue is null
                    and in_theme_revenue is null
                    and out_of_theme_revenue is null
                    and unclassified_revenue is null
                    and partition_residual is null
                )
                or (
                    extractor <> 'lane:unvisited:v1'
                    and cik is not null
                    and period_end is not null
                    and partition_id is not null
                    and consolidated_revenue is not null
                    and in_theme_revenue is not null
                    and out_of_theme_revenue is not null
                    and unclassified_revenue is not null
                    and partition_residual is not null
                )
            );
    end if;
end
$$;
