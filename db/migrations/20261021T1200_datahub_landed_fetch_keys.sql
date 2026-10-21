-- #1411 K3/K5/K7, #1418 B2: one persisted index of the fetch keys that already landed.
--
-- A fetch key is (seat, endpoint, canonical entity, as-of). Before the source gateway makes a
-- vendor or SEC call, it reads this table. A key that landed is served from the stored bytes
-- (raw.fetches, object storage) with no external call. The row survives a process restart, a
-- rolled-back tick and a second environment run, because the gateway writes it on its own
-- autocommit connection, like the call ledger.
--
-- The ledger gets the same key on every row it writes for a keyed call. The nightly verdict
-- fetch_keys_once counts ledger rows per key and per UTC day. A key with two successful rows in
-- one day is a duplicate fetch, and the verdict names it.
--
-- Backward compatible and replay-safe: the table is new, and the ledger column is added only when
-- it is absent (a replay on a settled database takes no lock).
create table if not exists staging.landed_fetch_keys (
    seat                text not null check (char_length(seat) > 0),
    endpoint            text not null check (char_length(endpoint) > 0),
    canonical_entity_id text not null check (char_length(canonical_entity_id) > 0),
    as_of               text not null check (char_length(as_of) > 0),
    raw_fetch_id        bigint not null references raw.fetches (id),
    payload_sha256      text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
    landed_at           timestamptz not null default clock_timestamp(),
    landed_run_key      text,
    reuse_count         bigint not null default 0 check (reuse_count >= 0),
    last_reused_at      timestamptz,
    primary key (seat, endpoint, canonical_entity_id, as_of)
);

comment on table staging.landed_fetch_keys is
    '#1411 K3: one row per fetch key that landed (seat, endpoint, canonical entity, as-of). The source gateway reads it before every keyed vendor or SEC call and serves a landed key from raw.fetches with no external call. reuse_count counts those reads.';

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'staging.api_call_ledger'::regclass
          and attname = 'fetch_key'
          and not attisdropped
    ) then
        alter table staging.api_call_ledger add column if not exists fetch_key text;
    end if;
end
$$;

comment on column staging.api_call_ledger.fetch_key is
    '#1411 K7: the canonical fetch key (seat|endpoint|entity|as-of) the call fetched, null for a call that has no key yet. A key with two ok rows on one UTC day is a duplicate fetch (nightly verdict fetch_keys_once).';

-- The verdict reads one UTC day of keyed rows.
do $$
begin
    if to_regclass('staging.ix_api_call_ledger_fetch_key') is null then
        create index if not exists ix_api_call_ledger_fetch_key
            on staging.api_call_ledger (called_at, fetch_key) where fetch_key is not null;
    end if;
end
$$;

-- /admin/datahub reads the landed keys through app_ops_reader. Conditional because CI applies
-- migrations before roles.sql creates the role; the guard keeps a replay lock-free.
do $$
begin
    if exists (select from pg_roles where rolname = 'app_ops_reader')
       and not has_table_privilege('app_ops_reader', 'staging.landed_fetch_keys', 'SELECT')
    then
        grant select on staging.landed_fetch_keys to app_ops_reader;
    end if;
end;
$$;
