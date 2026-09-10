-- #772 (init.md §7 module 6, §0 question 6): theme purity becomes a materialized factor
-- output — "who is the purest name under a given theme", one row per issuer per theme per
-- governed run.
--
-- The column that decides whether this table is honest is `consolidated_revenue`, and it is
-- here rather than derivable because the share MUST be read against it. A missed segment
-- silently raises every remaining segment's share, so a purity computed over "the segments
-- we happen to have" ranks the issuer with the worst extraction highest. The denominator
-- travels with the number, and `partition_residual` says how much of the whole the parts
-- missed, so a reader can refuse a row it does not believe instead of trusting it.
--
-- The three revenue masses are stated, not implied: in-theme, out-of-theme and
-- UNCLASSIFIED. The third is the one a naive schema drops. A theme share of 0.60 with 0.00
-- unclassified and one with 0.35 unclassified are different claims, and a table that cannot
-- tell them apart is ranking its own classifier coverage.
create table if not exists mart.issuer_theme_purity (
    run_id                    text not null,
    -- The issuer, in both the identity the KG uses and the one SEC filings are addressed
    -- by. Both, because the reader joins on issuer_id and the producer wrote by cik.
    issuer_id                 text not null check (issuer_id <> ''),
    cik                       integer not null check (cik > 0),
    -- Which theme this row answers. Part of the key: an issuer has one purity PER theme.
    theme_id                  text not null check (theme_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    theme                     text not null check (theme <> ''),
    -- The governed theme definition (`truealpha_contracts.theme_purity`). Two runs are
    -- comparable only under the same sha: the inclusion wording IS the question asked, so a
    -- reworded theme is a different measurement wearing the same name.
    definition_version        text not null check (definition_version <> ''),
    definition_sha256         text not null check (definition_sha256 ~ '^[0-9a-f]{64}$'),
    cutoff                    timestamptz not null,
    -- The fiscal period the segments describe, from the oracle the partition was checked
    -- against — so the parts and the total provably describe the same year.
    period_end                date not null,
    -- The accepted segment set this share was computed over. A reader can pull the exact
    -- rows back out of staging.issuer_segment_revenue_facts and recompute.
    partition_id              text not null check (partition_id ~ '^segment-partition:[0-9a-f]{64}$'),
    -- The share, 0-1. NULL when refused — never a zero, which would rank as "not in the
    -- theme at all" instead of "we could not say".
    theme_share               numeric check (theme_share is null or (theme_share >= 0 and theme_share <= 1)),
    -- THE DENOMINATOR. Not derivable from the masses below: the parts may miss it by
    -- partition_residual, and that gap is exactly what a wrong denominator would hide.
    consolidated_revenue      numeric not null check (consolidated_revenue > 0),
    in_theme_revenue          numeric not null,
    out_of_theme_revenue      numeric not null,
    -- Revenue the classifier declined to judge. Distinct from out-of-theme on purpose: a
    -- declined segment lowers confidence in the share, a judged one lowers the share.
    unclassified_revenue      numeric not null,
    partition_residual        numeric not null,
    segments                  integer not null check (segments >= 0),
    confidence                numeric not null check (confidence >= 0 and confidence <= 1),
    reason_codes              text[] not null default '{}',
    -- Who judged: the served model and prompt digest (`ModelInvocation.extractor`), or a
    -- rule id. Kept per row so a share's provenance survives a single-row read.
    extractor                 text not null check (extractor <> ''),
    -- The three §8 status dimensions (#747), written by this producer like every other
    -- factor row. Vocabularies are `truealpha_contracts.execution`'s enums.
    availability_status       text not null
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error')),
    source_evidence_status    text not null
        check (source_evidence_status in ('verified', 'degraded', 'rejected')),
    factor_validation_status  text not null
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated')),
    created_at                timestamptz not null default clock_timestamp(),
    primary key (run_id, issuer_id, theme_id),
    -- The masses account for the whole, by construction. A violation means the producer's
    -- arithmetic, not a policy choice, so the database refuses the row rather than
    -- publishing a share whose parts do not add up.
    check (in_theme_revenue + out_of_theme_revenue + unclassified_revenue = consolidated_revenue)
);

create index if not exists ix_issuer_theme_purity_ranking
    on mart.issuer_theme_purity (theme_id, cutoff desc, theme_share desc nulls last);

comment on table mart.issuer_theme_purity is
    '#772 (init.md §7 module 6): one theme-purity row per issuer per theme per governed run. theme_share is over consolidated_revenue, never over the classified parts — a missed segment would otherwise RAISE the share and rank the worst extraction as the purest name.';
comment on column mart.issuer_theme_purity.consolidated_revenue is
    'The denominator the share is over: the issuer''s own consolidated revenue, the same number the segment partition was accepted against. Stored so a reader can recompute rather than trust.';
comment on column mart.issuer_theme_purity.unclassified_revenue is
    'Revenue the classifier declined to judge. A share of 0.60 with 0.35 unclassified is a different claim from 0.60 with 0.00 — a reader that cannot see this is ranking classifier coverage.';
comment on column mart.issuer_theme_purity.theme_share is
    'NULL when refused (see reason_codes) — never zero, which reads as "none of this issuer is in the theme" rather than "we could not say".';

-- Read roles: mart_readonly is the App's reader (db/roles.sql's blanket grant covers
-- tables that existed when it ran, so a new one needs its own); app_ops_reader is the
-- operator surface. Both are SELECT only.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.issuer_theme_purity to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.issuer_theme_purity to app_ops_reader;
    end if;
end;
$$;
