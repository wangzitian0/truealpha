-- D2 E2: additive typed projections for the shared MVP medium-domain path.
--
-- The 0004 market_prices table cannot be upgraded honestly: existing rows have
-- no reviewed confidence and include adjusted prices. Seal it instead of
-- inventing evidence, and route new normalized records through the canonical
-- unadjusted, confidence-bearing projections below.

create or replace function staging.reject_legacy_market_price_insert()
returns trigger language plpgsql as $$
begin
    raise exception 'legacy staging.market_prices has no confidence contract; use staging.mvp_market_prices';
end;
$$;

drop trigger if exists trg_market_prices_reject_insert on staging.market_prices;
create trigger trg_market_prices_reject_insert
before insert on staging.market_prices
for each row execute function staging.reject_legacy_market_price_insert();

create table if not exists staging.mvp_market_prices (
    normalized_record_id  text primary key references staging.normalized_records(normalized_record_id),
    subject_kind          text not null check (subject_kind = 'listing'),
    subject_id            text not null,
    input_id              text not null,
    issuer_id             text not null,
    security_id           text not null,
    listing_id            text not null,
    share_class           text not null,
    exchange_mic          char(4) not null,
    ticker                text not null,
    calendar_id           text not null,
    calendar_version      text not null,
    trading_date          date not null,
    session_close_at      timestamptz not null,
    open                  numeric not null check (open > 0),
    high                  numeric not null check (high > 0),
    low                   numeric not null check (low > 0),
    close                 numeric not null check (close > 0),
    volume                bigint not null check (volume >= 0),
    currency              char(3) not null,
    price_basis           text not null check (price_basis = 'unadjusted'),
    confidence_policy_id  text not null,
    price_policy_id       text not null,
    valid_time            daterange not null,
    transaction_time      timestamptz not null,
    recorded_at           timestamptz not null,
    confidence            numeric not null check (confidence between 0 and 1),
    raw_ref               text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (listing_id = subject_id),
    check (valid_time = daterange(trading_date, trading_date, '[]')),
    check (high >= greatest(open, low, close)),
    check (low <= least(open, high, close)),
    check (transaction_time >= session_close_at),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_market_prices_asof
    on staging.mvp_market_prices (listing_id, trading_date, transaction_time desc, recorded_at desc);

create table if not exists staging.mvp_financial_facts (
    normalized_record_id  text primary key references staging.normalized_records(normalized_record_id),
    subject_kind          text not null check (subject_kind = 'issuer'),
    subject_id            text not null,
    entity_id             text not null,
    metric                text not null,
    value                 numeric,
    unit                  text not null,
    fiscal_period         text not null,
    source_metric         text not null,
    mapping_version       text not null,
    accession             text,
    form                  text,
    is_restatement        boolean not null,
    valid_time            daterange not null,
    transaction_time      timestamptz not null,
    recorded_at           timestamptz not null,
    confidence            numeric not null check (confidence between 0 and 1),
    raw_ref               text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (entity_id = subject_id),
    check (not isempty(valid_time)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_financial_facts_asof
    on staging.mvp_financial_facts (entity_id, metric, fiscal_period, transaction_time desc, recorded_at desc);

create table if not exists staging.mvp_corporate_actions (
    normalized_record_id          text primary key references staging.normalized_records(normalized_record_id),
    subject_kind                  text not null check (subject_kind = 'security'),
    subject_id                    text not null,
    action_id                     text not null,
    action_type                   text not null,
    security_id                  text not null,
    share_class                  text not null,
    source_instrument_ids         text[] not null,
    resulting_instrument_ids      text[] not null,
    source_listing_id             text,
    resulting_listing_id          text,
    declared_at                   timestamptz not null,
    ex_at                         timestamptz,
    effective_at                  timestamptz,
    record_at                     timestamptz,
    pay_at                        timestamptz,
    split_ratio_after_per_before  numeric,
    cash_amount_per_share         numeric,
    cash_currency                 char(3),
    old_symbol                    text,
    new_symbol                    text,
    delisting_reason              text,
    valid_time                    daterange not null,
    transaction_time              timestamptz not null,
    recorded_at                   timestamptz not null,
    confidence                    numeric not null check (confidence between 0 and 1),
    raw_ref                       text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (security_id = subject_id),
    check (not isempty(valid_time)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_corporate_actions_asof
    on staging.mvp_corporate_actions (security_id, transaction_time desc, recorded_at desc);

create table if not exists staging.mvp_universe_memberships (
    normalized_record_id  text primary key references staging.normalized_records(normalized_record_id),
    subject_kind          text not null,
    subject_id            text not null,
    membership_id         text not null,
    universe_id           text not null,
    valid_time            daterange not null,
    transaction_time      timestamptz not null,
    recorded_at           timestamptz not null,
    confidence            numeric not null check (confidence between 0 and 1),
    raw_ref               text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (subject_kind <> 'universe'),
    check (not isempty(valid_time)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_universe_memberships_asof
    on staging.mvp_universe_memberships (
        universe_id, subject_kind, subject_id, transaction_time desc, recorded_at desc
    );

create table if not exists staging.mvp_issuer_security_links (
    normalized_record_id                 text primary key references staging.normalized_records(normalized_record_id),
    subject_kind                         text not null check (subject_kind = 'issuer'),
    subject_id                           text not null,
    input_id                             text not null,
    issuer_id                            text not null,
    security_id                          text not null,
    security_kind                        text not null,
    share_class                          text,
    underlying_security_id               text,
    underlying_shares_per_security_unit  numeric not null check (underlying_shares_per_security_unit > 0),
    valid_time                           daterange not null,
    transaction_time                     timestamptz not null,
    recorded_at                          timestamptz not null,
    confidence                           numeric not null check (confidence between 0 and 1),
    raw_ref                              text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (issuer_id = subject_id),
    check (not isempty(valid_time)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_issuer_security_links_asof
    on staging.mvp_issuer_security_links (issuer_id, security_id, transaction_time desc, recorded_at desc);

create table if not exists staging.mvp_security_listing_links (
    normalized_record_id      text primary key references staging.normalized_records(normalized_record_id),
    subject_kind              text not null check (subject_kind = 'security'),
    subject_id                text not null,
    input_id                  text not null,
    security_id               text not null,
    listing_id                text not null,
    exchange_mic              char(4) not null,
    ticker                    text not null,
    listing_role              text not null,
    currency                  char(3) not null,
    timezone                  text not null,
    trading_calendar_id       text not null,
    trading_calendar_version  text not null,
    valid_time                daterange not null,
    transaction_time          timestamptz not null,
    recorded_at               timestamptz not null,
    confidence                numeric not null check (confidence between 0 and 1),
    raw_ref                   text not null check (raw_ref ~ '^raw\.fetches:[1-9][0-9]*$'),
    check (security_id = subject_id),
    check (not isempty(valid_time)),
    check (recorded_at >= transaction_time)
);

create index if not exists idx_mvp_security_listing_links_asof
    on staging.mvp_security_listing_links (security_id, listing_id, transaction_time desc, recorded_at desc);

create or replace function staging.validate_mvp_projection()
returns trigger language plpgsql as $$
declare
    normalized staging.normalized_records%rowtype;
    expected_type text := tg_argv[0];
begin
    select * into normalized
    from staging.normalized_records
    where normalized_record_id = new.normalized_record_id;

    if not found then
        raise exception 'MVP projection has no normalized record %', new.normalized_record_id
            using errcode = '23503';
    end if;
    if normalized.semantic_type_id <> expected_type
       or normalized.subject_kind <> new.subject_kind
       or normalized.subject_id <> new.subject_id
       or normalized.valid_time <> new.valid_time
       or normalized.transaction_time <> new.transaction_time
       or normalized.recorded_at <> new.recorded_at
       or normalized.confidence <> new.confidence
       or normalized.raw_ref <> new.raw_ref then
        raise exception 'MVP projection does not match normalized record %', new.normalized_record_id
            using errcode = '23514';
    end if;
    return new;
end;
$$;

do $$
declare
    projection record;
begin
    for projection in
        select * from (values
            ('mvp_market_prices', 'semantic.market-price'),
            ('mvp_financial_facts', 'semantic.financial-fact'),
            ('mvp_corporate_actions', 'semantic.corporate-action'),
            ('mvp_universe_memberships', 'semantic.universe-membership'),
            ('mvp_issuer_security_links', 'semantic.issuer-security-link'),
            ('mvp_security_listing_links', 'semantic.security-listing-link')
        ) as values_table(table_name, semantic_type_id)
    loop
        execute format(
            'drop trigger if exists %I on staging.%I',
            'trg_' || projection.table_name || '_validate',
            projection.table_name
        );
        execute format(
            'create trigger %I before insert on staging.%I '
            'for each row execute function staging.validate_mvp_projection(%L)',
            'trg_' || projection.table_name || '_validate',
            projection.table_name,
            projection.semantic_type_id
        );
        execute format(
            'drop trigger if exists %I on staging.%I',
            'trg_' || projection.table_name || '_append_only',
            projection.table_name
        );
        execute format(
            'create trigger %I before update or delete on staging.%I '
            'for each row execute function staging.reject_point_in_time_mutation()',
            'trg_' || projection.table_name || '_append_only',
            projection.table_name
        );
    end loop;
end $$;
