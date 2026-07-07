-- Point-in-time core (init.md Section 6).
-- valid_time = the period the data describes; transaction_time = when it became knowable.
-- A restatement always produces a new row (is_restatement), never an overwrite.

create table if not exists staging.kg_entities (
    id            text primary key,          -- our internal unified_id
    entity_type   text not null,             -- 'company' | 'etf' | 'analyst' | 'supply_chain_node'
    display_name  text not null
);

create table if not exists staging.kg_edges (
    id                bigint generated always as identity primary key,
    from_id           text not null references staging.kg_entities(id),
    to_id             text not null references staging.kg_entities(id),
    relation_type     text not null,          -- 'same_as' | 'supplies_to' | 'holds' | 'covers' | ...
    valid_time        daterange not null,
    transaction_time  timestamptz not null default now(),
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    source            text not null,
    raw_ref           text                    -- pointer back to the original record in the raw schema
);

create index if not exists idx_kg_edges_asof
    on staging.kg_edges (from_id, relation_type, transaction_time desc);

create table if not exists staging.financial_facts (
    id                bigint generated always as identity primary key,
    unified_id        text not null references staging.kg_entities(id),
    metric            text not null,          -- 'revenue' / 'gross_profit' / ...
    fiscal_period     text not null,          -- '2025Q4'
    valid_time        daterange not null,
    transaction_time  timestamptz not null default now(),
    value             numeric,
    -- MANDATORY on every row. Absorbs both extraction uncertainty (LLM-derived facts
    -- self-report a score) and source reliability (yfinance's lack of an SLA lives
    -- here, not in an ad hoc rule). Official filed data (SEC) defaults to 1.0.
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    source            text not null,          -- 'sec' | 'yfinance' | 'twelvedata' | 'moomoo'
    raw_ref           text,
    is_restatement    boolean not null default false
);

create index if not exists idx_financial_facts_asof
    on staging.financial_facts (unified_id, metric, fiscal_period, transaction_time desc);
