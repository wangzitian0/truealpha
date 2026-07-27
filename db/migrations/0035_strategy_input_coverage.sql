-- #496 (metric first): L2 per-issuer strategy-input coverage, persisted per
-- capture run. The quality report (0029) is append-only and is built BEFORE
-- strategy inputs are seeded inside the tick, so L2 lands as a sibling mart
-- table written right after seeding, in the same transaction. One row per
-- (run, issuer): how many of the frozen strategy definition's required input
-- keys are present at the cutoff, and which are missing — the funnel's L2
-- tile is avg(present/required); the drill-down IS the fix-list for the
-- coverage gaps this issue tracks.

create table if not exists mart.strategy_input_coverage (
    run_id          text not null,
    issuer_id       text not null,
    required_count  integer not null check (required_count > 0),
    present_count   integer not null check (present_count >= 0),
    missing_keys    text[] not null,
    created_at      timestamptz not null default clock_timestamp(),
    primary key (run_id, issuer_id),
    check (present_count <= required_count),
    check (cardinality(missing_keys) = required_count - present_count)
);

comment on table mart.strategy_input_coverage is
    '#496: per-issuer required-input completeness per capture run (L2 funnel metric). Append-only; missing_keys is the actionable gap list.';

drop trigger if exists reject_mutation on mart.strategy_input_coverage;
create trigger reject_mutation
before update or delete on mart.strategy_input_coverage
for each row execute function mart.reject_mutation();
