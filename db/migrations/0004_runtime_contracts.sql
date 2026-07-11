-- Runtime persistence and point-in-time replay contracts.
-- PostgreSQL is both the relational system of record and the shallow graph store.

create table if not exists raw.fetches (
    id                    bigint generated always as identity primary key,
    source                text not null,
    source_record_id      text not null,
    payload_sha256        text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
    object_uri            text not null check (object_uri like 's3://%'),
    content_type          text not null,
    byte_length           bigint not null check (byte_length >= 0),
    source_published_at   timestamptz,
    fetched_at            timestamptz not null,
    recorded_at           timestamptz not null default now(),
    metadata              jsonb not null default '{}'::jsonb,
    unique (source, source_record_id, payload_sha256),
    check (recorded_at >= fetched_at)
);

create index if not exists idx_raw_fetches_source_time
    on raw.fetches (source, source_record_id, fetched_at desc);

create or replace function raw.reject_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'raw records are append-only; insert a new payload vintage';
end;
$$;

drop trigger if exists trg_raw_fetches_append_only on raw.fetches;
create trigger trg_raw_fetches_append_only
before update or delete on raw.fetches
for each row execute function raw.reject_mutation();

-- A source identifier locates a source-specific KG entity node. Resolution
-- then traverses a point-in-time same_as edge to the unified entity; this is a
-- graph property table, not the retired flat symbol_mapping crosswalk.
create table if not exists staging.kg_identifiers (
    id                  bigint generated always as identity primary key,
    entity_id           text not null references staging.kg_entities(id),
    source              text not null,
    identifier_type     text not null,
    identifier_value    text not null,
    valid_time          daterange not null,
    transaction_time    timestamptz not null,
    recorded_at         timestamptz not null default now(),
    confidence          numeric not null check (confidence >= 0 and confidence <= 1),
    raw_ref             text not null,
    unique (source, identifier_type, identifier_value, transaction_time),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_kg_identifiers_asof
    on staging.kg_identifiers (source, identifier_type, identifier_value, transaction_time desc);

create table if not exists staging.market_prices (
    id                  bigint generated always as identity primary key,
    unified_id          text not null references staging.kg_entities(id),
    symbol              text not null,
    trading_date        date not null,
    open                numeric not null,
    high                numeric not null,
    low                 numeric not null,
    close               numeric not null,
    adjusted_close      numeric not null,
    volume              bigint not null check (volume >= 0),
    transaction_time    timestamptz not null,
    recorded_at         timestamptz not null default now(),
    source              text not null,
    raw_ref             text not null,
    unique (unified_id, trading_date, source, transaction_time),
    check (high >= greatest(open, low, close)),
    check (low <= least(open, high, close)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_market_prices_asof
    on staging.market_prices (unified_id, trading_date, transaction_time desc);

create table if not exists staging.analyst_rating_events (
    id                  bigint generated always as identity primary key,
    analyst_id          text not null references staging.kg_entities(id),
    company_id          text not null references staging.kg_entities(id),
    recommendation_at   timestamptz not null,
    transaction_time    timestamptz not null,
    vendor_updated_at   timestamptz,
    recorded_at         timestamptz not null default now(),
    rating              smallint not null check (rating between 1 and 5),
    target_price        numeric check (target_price >= 0),
    currency            text check (currency is null or currency ~ '^[A-Z]{3}$'),
    source_url          text,
    confidence          numeric not null check (confidence >= 0 and confidence <= 1),
    raw_ref             text not null,
    unique (analyst_id, company_id, recommendation_at, transaction_time),
    check (transaction_time >= recommendation_at),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_analyst_rating_events_asof
    on staging.analyst_rating_events (company_id, transaction_time desc, recommendation_at desc);

create table if not exists staging.fund_holding_facts (
    id                      bigint generated always as identity primary key,
    fund_id                 text not null references staging.kg_entities(id),
    holding_id              text references staging.kg_entities(id),
    holding_name            text not null,
    report_period           date not null,
    transaction_time        timestamptz not null,
    recorded_at             timestamptz not null default now(),
    cusip                    text,
    isin                     text,
    lei                      text,
    balance                  numeric,
    value_usd                numeric not null,
    percent_of_net_assets    numeric not null,
    confidence              numeric not null check (confidence >= 0 and confidence <= 1),
    raw_ref                 text not null,
    unique (fund_id, holding_name, report_period, transaction_time),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_fund_holding_facts_asof
    on staging.fund_holding_facts (fund_id, report_period, transaction_time desc);

create unique index if not exists uq_financial_facts_vintage
    on staging.financial_facts (
        unified_id,
        metric,
        fiscal_period,
        transaction_time,
        source,
        coalesce(raw_ref, '')
    );

create unique index if not exists uq_kg_edges_vintage
    on staging.kg_edges (
        from_id,
        to_id,
        relation_type,
        transaction_time,
        source,
        coalesce(raw_ref, '')
    );

-- Source facts must name when they became knowable. Ingestion time remains a
-- separate audit field and must never stand in for transaction time.
alter table staging.financial_facts
    alter column transaction_time drop default;
alter table staging.financial_facts
    add column if not exists recorded_at timestamptz not null default now();

alter table staging.kg_edges
    alter column transaction_time drop default;
alter table staging.kg_edges
    add column if not exists recorded_at timestamptz not null default now();

create or replace function staging.reject_point_in_time_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'point-in-time records are append-only; insert a new vintage';
end;
$$;

drop trigger if exists trg_financial_facts_append_only on staging.financial_facts;
create trigger trg_financial_facts_append_only before update or delete on staging.financial_facts
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_kg_edges_append_only on staging.kg_edges;
create trigger trg_kg_edges_append_only before update or delete on staging.kg_edges
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_kg_identifiers_append_only on staging.kg_identifiers;
create trigger trg_kg_identifiers_append_only before update or delete on staging.kg_identifiers
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_market_prices_append_only on staging.market_prices;
create trigger trg_market_prices_append_only before update or delete on staging.market_prices
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_analyst_rating_events_append_only on staging.analyst_rating_events;
create trigger trg_analyst_rating_events_append_only before update or delete on staging.analyst_rating_events
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_fund_holding_facts_append_only on staging.fund_holding_facts;
create trigger trg_fund_holding_facts_append_only before update or delete on staging.fund_holding_facts
for each row execute function staging.reject_point_in_time_mutation();
