-- ADR A1: the storage-neutral evidence chain, backed by Postgres.
--
-- `staging.evidence_nodes` / `staging.evidence_edges` are the append-only provenance graph
-- (nodes reuse the existing content-addressed identities; edges carry indexed trace keys).
-- `mart.current_pointer` is the governed head a consumer resolves; it advances forward only
-- and is read through `mart.current_pointer_head` under `mart_readonly`.

-- Generic append-only guards (clear messages per schema; reuse across this migration).
create or replace function staging.reject_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'evidence records are append-only; append a new node/edge instead';
end;
$$;

create or replace function mart.reject_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'current pointer advances are append-only; record a new advance instead';
end;
$$;

-- Nodes ------------------------------------------------------------------------------------
create table if not exists staging.evidence_nodes (
    node_id            text primary key,
    kind               text not null,
    content_sha256     text not null,
    valid_from         date not null,
    valid_to           date,
    transaction_time   timestamptz not null,
    recorded_at        timestamptz not null,
    supersedes_node_id text references staging.evidence_nodes (node_id),
    created_at         timestamptz not null default clock_timestamp(),
    constraint evidence_node_id_shape
        check (node_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'),
    constraint evidence_node_hash_matches_id
        check (split_part(node_id, ':', 2) = content_sha256),
    constraint evidence_node_period
        check (valid_to is null or valid_to >= valid_from),
    constraint evidence_node_kind
        check (kind in (
            'raw_fetch', 'source_vintage', 'normalized_observation', 'snapshot',
            'factor_invocation', 'materialized_result', 'capture_run', 'obligation',
            'quality_cell', 'release_manifest')),
    constraint evidence_node_kind_prefix
        check (split_part(node_id, ':', 1) = case kind
            when 'raw_fetch' then 'raw-fetch'
            when 'source_vintage' then 'source-vintage'
            when 'normalized_observation' then 'normalized-observation'
            when 'snapshot' then 'snapshot'
            when 'factor_invocation' then 'factor-invocation'
            when 'materialized_result' then 'materialized-result'
            when 'capture_run' then 'capture-run'
            when 'obligation' then 'capture-obligation'
            when 'quality_cell' then 'datahub-quality-cell'
            when 'release_manifest' then 'release-manifest'
        end),
    constraint evidence_node_no_self_supersede
        check (supersedes_node_id is null or supersedes_node_id <> node_id)
);

drop trigger if exists reject_mutation on staging.evidence_nodes;
create trigger reject_mutation
before update or delete on staging.evidence_nodes
for each row execute function staging.reject_mutation();

-- Edges ------------------------------------------------------------------------------------
create table if not exists staging.evidence_edges (
    edge_id          text primary key,
    content_sha256   text not null,
    from_kind        text not null,
    from_id          text not null references staging.evidence_nodes (node_id),
    to_kind          text not null,
    to_id            text not null references staging.evidence_nodes (node_id),
    relation         text not null,
    valid_from       date not null,
    valid_to         date,
    transaction_time timestamptz not null,
    recorded_at      timestamptz not null,
    created_at       timestamptz not null default clock_timestamp(),
    constraint evidence_edge_id_shape
        check (edge_id ~ '^evidence-edge:[0-9a-f]{64}$'),
    constraint evidence_edge_hash_matches_id
        check (split_part(edge_id, ':', 2) = content_sha256),
    constraint evidence_edge_not_self
        check (from_id <> to_id),
    constraint evidence_edge_relation
        check (relation in (
            'derived_from', 'selected_from', 'member_of', 'bound_to',
            'attested_by', 'supersedes')),
    constraint evidence_edge_period
        check (valid_to is null or valid_to >= valid_from)
);

create index if not exists evidence_edges_from_idx
    on staging.evidence_edges (from_kind, from_id);
create index if not exists evidence_edges_to_idx
    on staging.evidence_edges (to_kind, to_id);

drop trigger if exists reject_mutation on staging.evidence_edges;
create trigger reject_mutation
before update or delete on staging.evidence_edges
for each row execute function staging.reject_mutation();

-- Governed current pointer -----------------------------------------------------------------
create table if not exists mart.current_pointer (
    pointer_id        text primary key,
    content_sha256    text not null,
    environment       text not null,
    universe_id       text not null,
    universe_version  text not null,
    factor_id         text not null,
    target_run_id     text not null references staging.evidence_nodes (node_id),
    sequence          bigint not null,
    previous_run_id   text references staging.evidence_nodes (node_id),
    advanced_at       timestamptz not null,
    created_at        timestamptz not null default clock_timestamp(),
    constraint current_pointer_id_shape
        check (pointer_id ~ '^current-pointer:[0-9a-f]{64}$'),
    constraint current_pointer_hash_matches_id
        check (split_part(pointer_id, ':', 2) = content_sha256),
    constraint current_pointer_sequence_nonneg
        check (sequence >= 0),
    constraint current_pointer_target_is_run
        check (split_part(target_run_id, ':', 1) = 'capture-run'),
    constraint current_pointer_previous_is_run
        check (previous_run_id is null or split_part(previous_run_id, ':', 1) = 'capture-run'),
    constraint current_pointer_first_has_no_previous
        check ((sequence = 0) = (previous_run_id is null)),
    constraint current_pointer_advance_changes_run
        check (previous_run_id is null or previous_run_id <> target_run_id),
    constraint current_pointer_unique_advance
        unique (environment, universe_id, universe_version, factor_id, sequence)
);

drop trigger if exists reject_mutation on mart.current_pointer;
create trigger reject_mutation
before update or delete on mart.current_pointer
for each row execute function mart.reject_mutation();

-- The latest advance per governed key.
create or replace view mart.current_pointer_head as
select distinct on (environment, universe_id, universe_version, factor_id)
    pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
    target_run_id, sequence, previous_run_id, advanced_at
from mart.current_pointer
order by environment, universe_id, universe_version, factor_id, sequence desc;

-- `mart_readonly` receives select on new mart relations from db/roles.sql, which runs after
-- migrations. No grant here: the role does not exist yet during a fresh migration pass.
