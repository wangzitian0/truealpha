-- #772 (init.md §7 module 3, §0 question 3): supply chain relationship and exposure index
-- becomes a materialized factor output — one row per issuer per governed run.
create table if not exists mart.issuer_supply_chain_exposure (
    run_id                    text not null,
    issuer_id                 text not null check (issuer_id <> ''),
    cutoff                    timestamptz not null,
    exposure_score            numeric check (exposure_score is null or (exposure_score >= 0 and exposure_score <= 1)),
    direct_partners           integer not null default 0 check (direct_partners >= 0),
    suppliers_count           integer not null default 0 check (suppliers_count >= 0),
    customers_count           integer not null default 0 check (customers_count >= 0),
    max_partner_share         numeric not null default 0 check (max_partner_share >= 0 and max_partner_share <= 1),
    confidence                numeric not null default 0 check (confidence >= 0 and confidence <= 1),
    reason_codes              text[] not null default '{}',
    extractor                 text not null default 'graph:kg-edges:v1',
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
    if to_regclass('mart.ix_issuer_supply_chain_exposure_cutoff') is null then
        create index if not exists ix_issuer_supply_chain_exposure_cutoff
            on mart.issuer_supply_chain_exposure (cutoff desc, exposure_score desc nulls last);
    end if;
end
$$;

comment on table mart.issuer_supply_chain_exposure is
    '#772 (init.md §7 module 3): one supply chain exposure index row per issuer per governed run.';

do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.issuer_supply_chain_exposure to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.issuer_supply_chain_exposure to app_ops_reader;
    end if;
end;
$$;
