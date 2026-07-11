-- Operational tables (init.md Section 6, "other tables").

-- Every moomoo call goes through the global call-budget gateway; this is its ledger
-- The local budget is a defensive throttle, not a moomoo-side monthly quota.
create table if not exists staging.api_call_ledger (
    id           bigint generated always as identity primary key,
    source       text not null,               -- 'moomoo' for now
    endpoint     text not null,
    called_at    timestamptz not null default now(),
    caller       text not null,               -- which pipeline/module spent the quota
    ok           boolean not null
);

-- Only business-specific health metrics the Dagster UI doesn't already cover.
create table if not exists staging.ingestion_health_log (
    id           bigint generated always as identity primary key,
    source       text not null,
    metric       text not null,
    value        numeric,
    logged_at    timestamptz not null default now(),
    note         text
);
