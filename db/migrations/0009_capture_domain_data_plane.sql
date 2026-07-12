-- Issues #23/#62/#64: normalized capture domains and explicit empty-query
-- evidence. Domain records remain separate from capture observations so a
-- successful zero-event response never becomes a fabricated business fact.

-- Prices are instrument/listing observations, not issuer facts. This migration
-- intentionally fails if legacy rows exist: they cannot be assigned to a share
-- class without replaying their raw lineage.
do $$
begin
    if exists (select 1 from staging.market_prices limit 1) then
        raise exception 'market_prices contains issuer-keyed legacy rows; replay raw lineage before migration 0009';
    end if;
end $$;

alter table staging.market_prices add column if not exists instrument_id text
    references staging.instruments(instrument_id);
alter table staging.market_prices add column if not exists listing_id text;
alter table staging.market_prices add column if not exists currency text
    check (currency is null or currency ~ '^[A-Z]{3}$');
alter table staging.market_prices add column if not exists confidence numeric
    check (confidence is null or confidence between 0 and 1);
alter table staging.market_prices add column if not exists mapping_version text;
alter table staging.market_prices add column if not exists price_policy text
    check (price_policy is null or price_policy in ('raw_plus_actions', 'adjusted_no_actions'));

alter table staging.market_prices alter column instrument_id set not null;
alter table staging.market_prices alter column listing_id set not null;
alter table staging.market_prices alter column currency set not null;
alter table staging.market_prices alter column confidence set not null;
alter table staging.market_prices alter column mapping_version set not null;
alter table staging.market_prices alter column price_policy set not null;

do $$
begin
    if exists (
        select 1 from pg_constraint
        where conname = 'market_prices_unified_id_trading_date_source_transaction_time_key'
          and conrelid = 'staging.market_prices'::regclass
    ) then
        alter table staging.market_prices
            drop constraint market_prices_unified_id_trading_date_source_transaction_time_key;
    end if;
end $$;

create unique index if not exists uq_market_prices_instrument_vintage
    on staging.market_prices (
        instrument_id, listing_id, trading_date, source, transaction_time,
        raw_ref, mapping_version
    );

create table if not exists staging.corporate_actions (
    id                 bigint generated always as identity primary key,
    action_event_id    text not null,
    instrument_id      text not null references staging.instruments(instrument_id),
    listing_id         text not null,
    action_type        text not null check (action_type in (
        'split', 'cash_dividend', 'stock_dividend', 'spinoff', 'symbol_change', 'delisting'
    )),
    declaration_at     timestamptz,
    ex_date            date not null,
    effective_date     date not null,
    record_date        date,
    pay_date           date,
    ratio              numeric,
    cash_amount        numeric,
    currency           text check (currency is null or currency ~ '^[A-Z]{3}$'),
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (action_event_id, transaction_time, source, raw_ref, mapping_version),
    check (recorded_at >= transaction_time),
    check (
        (action_type = 'split' and ratio is not null)
        or (action_type = 'cash_dividend' and cash_amount is not null and currency is not null)
        or action_type not in ('split', 'cash_dividend')
    )
);

create index if not exists idx_corporate_actions_asof
    on staging.corporate_actions (instrument_id, ex_date, transaction_time desc);

create table if not exists staging.forecast_facts (
    id                 bigint generated always as identity primary key,
    issuer_id          text not null references staging.kg_entities(id),
    metric             text not null,
    forecast_period    text not null,
    estimate           numeric not null,
    estimate_low       numeric,
    estimate_high      numeric,
    currency           text check (currency is null or currency ~ '^[A-Z]{3}$'),
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    source_metric      text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (issuer_id, metric, forecast_period, transaction_time, source, raw_ref, mapping_version),
    check (recorded_at >= transaction_time),
    check (estimate_low is null or estimate_low <= estimate),
    check (estimate_high is null or estimate <= estimate_high)
);

create index if not exists idx_forecast_facts_asof
    on staging.forecast_facts (issuer_id, metric, forecast_period, transaction_time desc);

create table if not exists staging.company_guidance (
    id                 bigint generated always as identity primary key,
    issuer_id          text not null references staging.kg_entities(id),
    metric             text not null,
    forecast_period    text not null,
    range_low          numeric,
    range_high         numeric,
    unit               text not null,
    statement          text not null,
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    evidence_span      text not null,
    unique (issuer_id, metric, forecast_period, transaction_time, raw_ref, mapping_version),
    check (recorded_at >= transaction_time),
    check (range_low is not null or range_high is not null),
    check (range_low is null or range_high is null or range_low <= range_high)
);

create index if not exists idx_company_guidance_asof
    on staging.company_guidance (issuer_id, forecast_period, transaction_time desc);

create table if not exists staging.filing_documents (
    id                 bigint generated always as identity primary key,
    issuer_id          text not null references staging.kg_entities(id),
    accession          text not null,
    form               text not null,
    filing_period      date,
    document_name      text not null,
    document_sha256    text not null check (document_sha256 ~ '^[0-9a-f]{64}$'),
    source_url         text not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    unique (issuer_id, accession, document_name, raw_ref, mapping_version),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_filing_documents_asof
    on staging.filing_documents (issuer_id, form, transaction_time desc);

create table if not exists staging.filing_extractions (
    id                  bigint generated always as identity primary key,
    semantic_record_id  text not null,
    issuer_id           text not null references staging.kg_entities(id),
    filing_document_id  bigint not null references staging.filing_documents(id),
    extraction_type     text not null,
    payload             jsonb not null,
    evidence_span       text not null,
    extractor_version   text not null,
    review_state        text not null check (review_state in ('rule_verified', 'human_verified', 'unreviewed')),
    valid_time          daterange not null,
    transaction_time    timestamptz not null,
    recorded_at         timestamptz not null default now(),
    confidence          numeric not null check (confidence between 0 and 1),
    source              text not null,
    raw_ref             text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version     text not null,
    unique (semantic_record_id, transaction_time, raw_ref, mapping_version),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_filing_extractions_asof
    on staging.filing_extractions (issuer_id, extraction_type, transaction_time desc);

create table if not exists staging.segment_facts (
    id                 bigint generated always as identity primary key,
    issuer_id          text not null references staging.kg_entities(id),
    segment_type       text not null,
    segment_name       text not null,
    metric             text not null,
    fiscal_period      text not null,
    value              numeric not null,
    unit               text not null,
    valid_time         daterange not null,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null default now(),
    confidence         numeric not null check (confidence between 0 and 1),
    source             text not null,
    raw_ref            text not null check (raw_ref ~ '^raw.fetches:[0-9]+$'),
    mapping_version    text not null,
    taxonomy_version   text not null,
    unique (
        issuer_id, segment_type, segment_name, metric, fiscal_period,
        transaction_time, source, raw_ref, mapping_version
    ),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_segment_facts_asof
    on staging.segment_facts (issuer_id, fiscal_period, transaction_time desc);

-- A normalized observation proves that a query ran and describes whether it
-- produced domain rows. A complete_empty observation is evidence of absence;
-- it is never projected as a financial/action/relationship fact.
create table if not exists staging.capture_observations (
    id                    bigint generated always as identity primary key,
    run_id                text not null,
    subject_id            text not null,
    domain                text not null,
    partition_key         text not null,
    outcome               text not null check (outcome in ('complete_records', 'complete_empty', 'failed')),
    raw_refs              jsonb not null,
    domain_record_ids     jsonb not null,
    required_fields       jsonb not null,
    observed_fields       jsonb not null,
    min_knowable_at       timestamptz,
    max_knowable_at       timestamptz,
    observed_at           timestamptz not null,
    recorded_at           timestamptz not null default now(),
    confidence            numeric not null check (confidence between 0 and 1),
    source                text not null,
    mapping_version       text not null,
    detail                text,
    unique (run_id, subject_id, domain, partition_key),
    check (jsonb_typeof(raw_refs) = 'array'),
    check (jsonb_array_length(raw_refs) > 0),
    check (jsonb_typeof(domain_record_ids) = 'array'),
    check (recorded_at >= observed_at),
    check (max_knowable_at is null or max_knowable_at <= observed_at)
);

alter table staging.analyst_rating_events add column if not exists action text;
alter table staging.analyst_rating_events add column if not exists source text;
alter table staging.analyst_rating_events add column if not exists mapping_version text;

alter table staging.financial_facts add column if not exists issuer_category text;

drop trigger if exists trg_corporate_actions_append_only on staging.corporate_actions;
create trigger trg_corporate_actions_append_only before update or delete on staging.corporate_actions
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_forecast_facts_append_only on staging.forecast_facts;
create trigger trg_forecast_facts_append_only before update or delete on staging.forecast_facts
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_company_guidance_append_only on staging.company_guidance;
create trigger trg_company_guidance_append_only before update or delete on staging.company_guidance
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_filing_documents_append_only on staging.filing_documents;
create trigger trg_filing_documents_append_only before update or delete on staging.filing_documents
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_filing_extractions_append_only on staging.filing_extractions;
create trigger trg_filing_extractions_append_only before update or delete on staging.filing_extractions
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_segment_facts_append_only on staging.segment_facts;
create trigger trg_segment_facts_append_only before update or delete on staging.segment_facts
for each row execute function staging.reject_point_in_time_mutation();

drop trigger if exists trg_capture_observations_append_only on staging.capture_observations;
create trigger trg_capture_observations_append_only before update or delete on staging.capture_observations
for each row execute function staging.reject_point_in_time_mutation();
