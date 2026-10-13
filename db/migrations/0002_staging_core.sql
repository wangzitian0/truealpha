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

do $$
begin
    if to_regclass('staging.idx_kg_edges_asof') is null then
        create index if not exists idx_kg_edges_asof
            on staging.kg_edges (from_id, relation_type, transaction_time desc);
    end if;
end
$$;

-- #1258: staging.financial_facts and idx_financial_facts_asof are retired.
-- 20261013T0700_datahub_retire_staging_financial_facts.sql drops them in deployed databases.
