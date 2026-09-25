-- #771 (init.md §7 module 4, §0 question 4): analyst track record and consensus ratings
-- becomes a materialized factor output — one row per issuer per governed run.
create table if not exists mart.issuer_analyst_ratings (
    run_id                    text not null,
    issuer_id                 text not null check (issuer_id <> ''),
    cutoff                    timestamptz not null,
    consensus_rating          numeric check (consensus_rating is null or (consensus_rating >= 1 and consensus_rating <= 5)),
    analysts_count            integer not null default 0 check (analysts_count >= 0),
    buy_count                 integer not null default 0 check (buy_count >= 0),
    hold_count                integer not null default 0 check (hold_count >= 0),
    sell_count                integer not null default 0 check (sell_count >= 0),
    confidence                numeric not null default 0 check (confidence >= 0 and confidence <= 1),
    reason_codes              text[] not null default '{}',
    extractor                 text not null default 'origin:moomoo:v1',
    availability_status       text not null
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error')),
    source_evidence_status    text not null
        check (source_evidence_status in ('verified', 'degraded', 'rejected')),
    factor_validation_status  text not null
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated')),
    created_at                timestamptz not null default clock_timestamp(),
    primary key (run_id, issuer_id)
);

do $$
begin
    if to_regclass('mart.ix_issuer_analyst_ratings_cutoff') is null then
        create index if not exists ix_issuer_analyst_ratings_cutoff
            on mart.issuer_analyst_ratings (cutoff desc, consensus_rating desc nulls last);
    end if;
end
$$;

comment on table mart.issuer_analyst_ratings is
    '#771 (init.md §7 module 4): one analyst rating and consensus row per issuer per governed run.';

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.issuer_analyst_ratings to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.issuer_analyst_ratings to app_ops_reader;
    end if;
end;
$$;
