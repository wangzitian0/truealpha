-- #729 (owner decision 2026-09-04, init.md §1 rule 6 generalized): staging.api_call_ledger
-- becomes the external call ledger for EVERY source, not moomoo's private quota log.
--
-- One row per outbound request, success or failure. What the new columns are for:
--   status_code / error      the vendor's verdict; a rate-limited or 4xx request is a
--                            failed request that still spent quota, and it now says why
--   duration_ms              the vendor's latency, per call
--   request_uri              the URL asked, credential query values blanked by the writer
--   payload_sha256 / byte_length
--                            the digest of the exact response body — the join key to
--                            raw.fetches.payload_sha256, so a recorded call dereferences to
--                            the bytes the capture landed (owner requirement: every request
--                            traceable to what we stored) or visibly to nothing
--   cost                     1 call today; tokens for a model provider when #70 lands
--   capacity_window_id       the vendor window the call was charged to
--                            ('twelvedata:day:2026-09-04'), so used-vs-declared is one GROUP BY
--   run_key                  the Dagster run that made the call
--
-- Backward compatible and replay-safe (ci-db applies the chain three times): nullable
-- adds with a default only where the column is semantically total (cost = 1 call), and
-- every object guarded with IF NOT EXISTS. Existing moomoo rows keep their meaning.

alter table staging.api_call_ledger
    add column if not exists status_code        integer,
    add column if not exists error              text,
    add column if not exists duration_ms        integer,
    add column if not exists request_uri        text,
    add column if not exists payload_sha256     text,
    add column if not exists byte_length        bigint,   -- raw.fetches.byte_length is bigint
    add column if not exists cost               numeric not null default 1,
    add column if not exists capacity_window_id text,
    add column if not exists run_key            text;

do $$ begin
    if not exists (select 1 from pg_constraint
                   where conname = 'api_call_ledger_payload_sha256_check'
                     and conrelid = 'staging.api_call_ledger'::regclass) then
        alter table staging.api_call_ledger
            add constraint api_call_ledger_payload_sha256_check
            check (payload_sha256 is null or payload_sha256 ~ '^[0-9a-f]{64}$');
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'api_call_ledger_cost_check'
                     and conrelid = 'staging.api_call_ledger'::regclass) then
        alter table staging.api_call_ledger
            add constraint api_call_ledger_cost_check check (cost >= 0);
    end if;
end $$;

-- The dashboard's two reads: "today, per source" and "the last N calls".
create index if not exists ix_api_call_ledger_source_called_at
    on staging.api_call_ledger (source, called_at desc);
create index if not exists ix_api_call_ledger_called_at
    on staging.api_call_ledger (called_at desc);
-- The traceability join in both directions: ledger row -> landed bytes, and the
-- reverse question "which request produced this fetch".
create index if not exists ix_api_call_ledger_payload_sha256
    on staging.api_call_ledger (payload_sha256) where payload_sha256 is not null;
create index if not exists ix_raw_fetches_payload_sha256
    on raw.fetches (payload_sha256);

comment on table staging.api_call_ledger is
    'External call ledger (#729): one row per request to any vendor or model provider, success or failure. '
    'payload_sha256 joins raw.fetches.payload_sha256; failed rows carry the vendor error.';

-- /admin/datahub reads the ledger through app_ops_reader (Capacity + Traffic sections).
-- Conditional here because CI applies migrations before roles.sql exists on a fresh
-- database; roles.sql carries the permanent grant.
do $$ begin
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant usage on schema staging to app_ops_reader;
        grant select on staging.api_call_ledger to app_ops_reader;
    end if;
end $$;
