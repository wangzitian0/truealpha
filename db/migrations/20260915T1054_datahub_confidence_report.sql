-- The datahub CONFIDENCE & ACCURACY report (owner standard, 2026-09-15): per governed head,
-- every (metric family, subject) cell graded high / medium / low / missing from what its
-- origins asserted, the per-family aggregates, a ten-subject sample across every origin,
-- and the accuracy section (yahoo/twelve-data close agreement; SEC oracle re-derivation
-- of a sample of fundamentals). Append-only and content-addressed like
-- mart.datahub_quality_report: the report_id IS the sha256 of the canonical payload, so a
-- replay over the same head produces the same row and a changed grade is a new row.
create table if not exists mart.datahub_confidence_report (
    report_id        text primary key
        check (report_id ~ '^datahub-confidence-report:[0-9a-f]{64}$'),
    content_sha256   text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    universe_id      text not null,
    run_id           text not null check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    cutoff           timestamptz not null,
    payload          jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at       timestamptz not null default clock_timestamp(),
    constraint datahub_confidence_report_hash_matches_id
        check (split_part(report_id, ':', 2) = content_sha256)
);

create index if not exists ix_datahub_confidence_report_universe
    on mart.datahub_confidence_report (universe_id, created_at desc);

comment on table mart.datahub_confidence_report is
    'Per governed head: every (metric family, subject) cell banded high (>=2 independent origins reconciled agreed) / medium (>=2 origins present: no policy, disagreement, or one lineage) / low (one origin) / missing, with per-family aggregates, a sampled cross-origin view and the accuracy oracle. The stored observation confidence column is a constant and is not read by the bands (payload.metadata.stored_confidence).';

drop trigger if exists reject_mutation on mart.datahub_confidence_report;
create trigger reject_mutation
before update or delete on mart.datahub_confidence_report
for each row execute function mart.reject_mutation();

-- Read roles: mart_readonly (the blanket grant in roles.sql covers only tables that existed
-- when the role was created) and app_ops_reader (the /admin/datahub dashboard's ops reader).
-- Conditional because CI applies migrations before db/roles.sql creates the roles.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.datahub_confidence_report to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.datahub_confidence_report to app_ops_reader;
    end if;
end;
$$;
