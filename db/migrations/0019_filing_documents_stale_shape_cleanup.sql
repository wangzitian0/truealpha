-- #457: staging/production already had an older-shaped staging.filing_documents
-- (id bigint PK, filing_period, document_name, document_sha256, source_url, source —
-- from a since-consolidated earlier migration, predating 0019_mvp_filing_document.sql)
-- when 0019 was later authored. `create table if not exists` there is a no-op against
-- that stale shape, so 0019 fails at `create index ... report_period does not exist`.
--
-- A NEW migration rather than an edit to 0019 itself: 0019's file bytes are pinned by
-- apps/data-engine/src/data_engine/mvp_assets.py's content-addressed
-- MvpNormalizationHandoff (migration_sha256/migration_set_sha256), which cascades into
-- the D1_RUNTIME_HANDOFF_SHA256 fixture other tests assert against — editing 0019 in
-- place would drift that accepted evidence chain for no reason. This file sorts before
-- it (0019_f... < 0019_m...) purely by filename, so apply_migrations.sh's glob loop
-- runs this first and 0019 finds a clean slate.
--
-- Nothing in the current app targets the old shape:
-- apps/data-engine/src/data_engine/mvp_repository.py already writes
-- report_period/content_sha256/normalized_record_id only. The old table's data (62
-- rows staging, 0 prod) and its dependent staging.filing_extractions (also undefined
-- by any current migration, referenced only in a test) are orphaned.
--
-- Idempotent: only fires once, when the table exists AND lacks report_period (the
-- stale pre-0019 shape); once 0019 has created the new shape, this is a no-op forever.
do $$
begin
    if exists (
        select 1 from information_schema.tables
        where table_schema = 'staging' and table_name = 'filing_documents'
    ) and not exists (
        select 1 from information_schema.columns
        where table_schema = 'staging' and table_name = 'filing_documents'
          and column_name = 'report_period'
    ) then
        drop table staging.filing_documents cascade;
    end if;
end $$;
