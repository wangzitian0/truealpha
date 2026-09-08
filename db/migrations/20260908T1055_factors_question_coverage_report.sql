-- #748 (init.md rule 24): the weekly question-coverage report — per governed universe and
-- init.md §0 question, how many issuers are answered / unavailable (by reason) / missing
-- (no column exists yet). Append-only so the milestone's progress is a series, not a comment.
create table if not exists mart.question_coverage_report (
    report_id             text primary key
        check (report_id ~ '^question-coverage-report:[0-9a-f]{64}$'),
    content_sha256        text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    universe_id           text not null,
    run_id                text not null,
    cutoff                timestamptz not null,
    requirements_sha256   text not null check (requirements_sha256 ~ '^[0-9a-f]{64}$'),
    payload               jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at            timestamptz not null default clock_timestamp()
);
create index if not exists ix_question_coverage_report_universe
    on mart.question_coverage_report (universe_id, created_at desc);
comment on table mart.question_coverage_report is
    '#748: weekly per-universe coverage of the six init.md questions, compiled from QUESTION_REQUIREMENTS (expected) left-joined with the wide row''s §8 status dimensions (observed); requirements_sha256 pins the expectation the counts were made under.';

-- Read roles: mart_readonly (blanket grant in roles.sql only covers tables that existed when
-- the role was created) and app_ops_reader (the /admin/datahub dashboard's ops reader).
-- Conditional because CI applies migrations before db/roles.sql creates the roles.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.question_coverage_report to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.question_coverage_report to app_ops_reader;
    end if;
end;
$$;
