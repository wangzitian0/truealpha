-- Raw landing zone (init.md Section 2.1, L0/L1): every fetched payload lands here
-- verbatim before any normalization. Append-only and immutable — a re-fetch of the
-- same thing is a NEW row (new vintage), never an update; staging normalizers point
-- back here via raw_ref = 'raw.fetches:<id>'. Deliberately plain SQL rather than a
-- dlt-managed table: dlt's value is in the raw→staging normalization step (frozen
-- contracts, schema alerts — init.md Section 1 rule 7), not in this landing zone,
-- and its auto-evolved column layout would fight the "raw is immutable" rule.

create table if not exists raw.fetches (
    id           bigint generated always as identity primary key,
    source       text not null,          -- 'sec' | 'sec_nport' | 'moomoo' | 'openfigi' | 'yahoo' ...
    endpoint     text not null,          -- source-local endpoint name, e.g. 'companyfacts', 'get_financials_statements'
    entity_key   text not null,          -- source-local id the fetch was keyed on: CIK, moomoo code ('HK.00700'),
                                         -- ETF ticker, batch label — NOT a unified KG id; that mapping lives in
                                         -- staging.kg_edges, resolved later
    params       jsonb,                  -- request parameters needed to reproduce the call
    payload      jsonb,                  -- parsed JSON payload (JSON APIs, serialized DataFrames)
    content      text,                   -- raw text payload where JSON doesn't apply (N-PORT XML, filing HTML)
    fetched_at   timestamptz not null default now(),
    check (payload is not null or content is not null)
);

-- Serves both "latest payload for X" and the sweep resume check
-- ("was X already fetched successfully?").
create index if not exists idx_raw_fetches_lookup
    on raw.fetches (source, endpoint, entity_key, fetched_at desc);

-- Relationship attributes for KG edges (e.g. 'holds' weight: {"pct_val": 5.27,
-- "report_period": "2026-03-31"}). Nullable — same_as edges don't need it. The
-- ETF-virtual-company factor (init.md Section 7 module 5) reads holding weights
-- from here rather than re-parsing N-PORT XML out of raw.
alter table staging.kg_edges add column if not exists properties jsonb;
