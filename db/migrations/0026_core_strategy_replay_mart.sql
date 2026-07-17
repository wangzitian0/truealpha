-- First mart tables for the Core Strategy replay (#26).
--
-- `mart` (schema created in 0001_schemas.sql) has had zero tables until now
-- -- db/roles.sql already grants mart_readonly select on the schema and its
-- future tables via `alter default privileges`, so no additional grant is
-- needed here.
--
-- Scope: this only creates somewhere real for a future writer to land rows;
-- it does not itself wire apps/data-engine/scripts/run_strategy_smoke.py or
-- any Dagster asset to write here, and it does not imply the replay's input
-- facts are real captured data yet (see #26's tracked gaps). Table shape
-- mirrors the `Decision` dataclass run_strategy_smoke.py already computes,
-- so a future writer has no reshaping to do.
--
-- Monetary/ratio values use `numeric`, never float (init.md architecture
-- red line). Rows are immutable evidence: once written, a strategy
-- decision is never updated or deleted, only superseded by a new run --
-- mirrors staging's `reject_point_in_time_mutation` pattern.

create table if not exists mart.strategy_runs (
    strategy_run_id             text primary key check (strategy_run_id ~ '^strategy-run:[0-9a-f]{64}$'),
    content_sha256              text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    strategy_key                text not null,
    strategy_version            text not null,
    definition_content_sha256   text not null check (definition_content_sha256 ~ '^[0-9a-f]{64}$'),
    corpus_sha256                text not null check (corpus_sha256 ~ '^[0-9a-f]{64}$'),
    claim_ceiling                text not null,
    executed_at                  timestamptz not null,
    created_at                   timestamptz not null default now()
);

create table if not exists mart.strategy_decisions (
    strategy_decision_id                text primary key check (strategy_decision_id ~ '^strategy-decision:[0-9a-f]{64}$'),
    content_sha256                      text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    strategy_run_id                     text not null references mart.strategy_runs(strategy_run_id),
    issuer_id                           text not null,
    cutoff_at                           timestamptz not null,
    capital_adjusted_labor_efficiency   numeric,
    tier                                text,
    current_price_to_sales              numeric,
    target_price_to_sales               numeric,
    valuation_gap                       numeric,
    eligible                            boolean not null,
    outcome                             text not null,
    exclusion_reason                    text,
    rank                                integer,
    target_weight                       numeric,
    created_at                          timestamptz not null default now(),
    unique (strategy_run_id, issuer_id, cutoff_at)
);

create index if not exists idx_strategy_decisions_run on mart.strategy_decisions(strategy_run_id);
create index if not exists idx_strategy_decisions_issuer_cutoff on mart.strategy_decisions(issuer_id, cutoff_at);

create or replace function mart.reject_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'mart strategy evidence is append-only; insert a new run rather than mutating this row';
end;
$$;

drop trigger if exists trg_strategy_runs_append_only on mart.strategy_runs;
create trigger trg_strategy_runs_append_only before update or delete on mart.strategy_runs
for each row execute function mart.reject_mutation();

drop trigger if exists trg_strategy_decisions_append_only on mart.strategy_decisions;
create trigger trg_strategy_decisions_append_only before update or delete on mart.strategy_decisions
for each row execute function mart.reject_mutation();
