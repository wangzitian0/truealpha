-- Issue #57/#63: issuer facts and traded instruments are different identities.
-- Registries hold stable IDs; every assertion and membership is append-only,
-- bitemporal, confidence-bearing, mapping-versioned, and raw-lineage backed.

create table if not exists staging.instruments (
    instrument_id     text primary key,
    instrument_type   text not null,
    display_name      text not null,
    created_at        timestamptz not null default now(),
    check (instrument_id like 'instrument:%')
);

create table if not exists staging.instrument_issuer_links (
    id                 bigint generated always as identity primary key,
    instrument_id      text not null references staging.instruments(instrument_id),
    issuer_id          text not null references staging.kg_entities(id),
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (instrument_id, issuer_id, transaction_time, source, raw_ref, mapping_version),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_instrument_issuer_links_asof
    on staging.instrument_issuer_links (instrument_id, transaction_time desc);

create table if not exists staging.instrument_identifiers (
    id                 bigint generated always as identity primary key,
    instrument_id      text not null references staging.instruments(instrument_id),
    identifier_type    text not null,
    identifier_value   text not null,
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (identifier_type, identifier_value, transaction_time, source, raw_ref, mapping_version),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_instrument_identifiers_asof
    on staging.instrument_identifiers (identifier_type, identifier_value, transaction_time desc);

create table if not exists staging.listings (
    id                 bigint generated always as identity primary key,
    listing_id         text not null,
    instrument_id      text not null references staging.instruments(instrument_id),
    venue_code         text not null,
    ticker             text not null,
    currency           text not null check (currency ~ '^[A-Z]{3}$'),
    trading_timezone   text not null,
    trading_calendar   text not null,
    price_policy       text not null check (price_policy in ('raw_plus_actions', 'adjusted_no_actions')),
    is_primary         boolean not null,
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (listing_id, transaction_time, source, raw_ref, mapping_version),
    check (listing_id like 'listing:%'),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_listings_instrument_asof
    on staging.listings (instrument_id, transaction_time desc);

create table if not exists staging.universe_memberships (
    id                 bigint generated always as identity primary key,
    universe_id        text not null,
    universe_version   text not null,
    fund_id            text references staging.kg_entities(id),
    issuer_id          text not null references staging.kg_entities(id),
    instrument_id      text not null references staging.instruments(instrument_id),
    listing_id         text,
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (universe_id, universe_version, instrument_id, transaction_time, raw_ref, mapping_version),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_universe_memberships_asof
    on staging.universe_memberships (universe_id, universe_version, transaction_time desc);

-- Existing holding rows remain valid historical evidence. New writers fill the
-- instrument/listing and classification columns; legacy rows are intentionally
-- nullable rather than rewritten in place.
alter table staging.fund_holding_facts add column if not exists instrument_id text
    references staging.instruments(instrument_id);
alter table staging.fund_holding_facts add column if not exists listing_id text;
alter table staging.fund_holding_facts add column if not exists asset_type text;
alter table staging.fund_holding_facts add column if not exists currency text
    check (currency is null or currency ~ '^[A-Z]{3}$');
alter table staging.fund_holding_facts add column if not exists mapping_version text;

drop index if exists staging.uq_fund_holding_line_vintage;
create unique index if not exists uq_fund_holding_line_vintage
    on staging.fund_holding_facts (
        fund_id,
        report_period,
        transaction_time,
        coalesce(instrument_id, ''),
        coalesce(isin, ''),
        coalesce(cusip, ''),
        raw_ref,
        coalesce(mapping_version, 'legacy:0')
    );

drop trigger if exists trg_instruments_registry_no_mutation on staging.instruments;
create trigger trg_instruments_registry_no_mutation before update or delete on staging.instruments
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_instrument_issuer_links_append_only on staging.instrument_issuer_links;
create trigger trg_instrument_issuer_links_append_only before update or delete on staging.instrument_issuer_links
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_instrument_identifiers_append_only on staging.instrument_identifiers;
create trigger trg_instrument_identifiers_append_only before update or delete on staging.instrument_identifiers
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_listings_append_only on staging.listings;
create trigger trg_listings_append_only before update or delete on staging.listings
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_universe_memberships_append_only on staging.universe_memberships;
create trigger trg_universe_memberships_append_only before update or delete on staging.universe_memberships
for each row execute function staging.reject_point_in_time_mutation();
