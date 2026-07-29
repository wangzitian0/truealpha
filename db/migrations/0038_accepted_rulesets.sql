-- #496: the governed pointer that says which published ruleset a run must use.
--
-- Concept mappings became content-addressed contract objects (staging.contract_objects)
-- so a correction is an insert rather than a deploy. That only works if the runtime can
-- find the accepted one — and "the newest row of that kind" is exactly the bare mutable
-- latest the architecture forbids as a read path.
--
-- So the pointer is explicit and append-only: advancing it is a row, reverting is another
-- row, and the history of which rules were in force when is queryable rather than
-- reconstructed. A run resolves the head once and records the exact ruleset hash it used,
-- so a number stays explainable after the pointer moves on.

create table if not exists staging.accepted_rulesets (
    kind            text not null check (kind <> ''),
    contract_id     text not null check (contract_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'),
    sequence        integer not null check (sequence > 0),
    -- Why this ruleset was accepted. Required: a mapping change with no stated reason is
    -- indistinguishable from an accident, and this table is the audit trail for a plane
    -- that deliberately bypasses code review.
    note            text not null check (note <> ''),
    advanced_at     timestamptz not null default clock_timestamp(),
    primary key (kind, sequence)
);

comment on table staging.accepted_rulesets is
    '#496: append-only governed pointer for published rulesets (e.g. concept-mapping). The head is max(sequence) per kind; never read "the latest contract object" directly.';

drop trigger if exists reject_mutation on staging.accepted_rulesets;
create trigger reject_mutation
before update or delete on staging.accepted_rulesets
for each row execute function raw.reject_mutation();

create or replace view staging.accepted_ruleset_head as
select distinct on (kind) kind, contract_id, sequence, note, advanced_at
from staging.accepted_rulesets
order by kind, sequence desc;

comment on view staging.accepted_ruleset_head is
    'The in-force ruleset per kind. Resolving through this view, not through contract_objects directly, is what keeps the read governed.';

-- Extend the contract-kind allowlist for published concept mappings.
--
-- Derived from whatever constraint is in force rather than re-declaring the list. The
-- first version of this migration copied 0017's clauses and silently dropped the two
-- kinds 0018 had added since — a whole-list rewrite cannot see additions it predates, and
-- the next such migration would have dropped `concept-mapping` the same way. Widening the
-- existing expression makes that class of mistake impossible.
--
-- The allowlist itself stays deliberate: an arbitrary kind would let anything masquerade
-- as a governed contract, so adding a KIND is a reviewed schema change even though
-- publishing an INSTANCE of one is not.
do $$
declare
    existing_def text;
begin
    select pg_get_constraintdef(constraint_row.oid) into existing_def
    from pg_constraint as constraint_row
    join pg_class as table_row on table_row.oid = constraint_row.conrelid
    join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
    where schema_row.nspname = 'staging'
      and table_row.relname = 'contract_objects'
      and constraint_row.conname = 'contract_objects_kind_identity_check';

    if existing_def is null then
        raise exception 'contract_objects_kind_identity_check is missing; 0017/0018 must apply first';
    end if;

    -- Idempotent: re-running the migration file must be a no-op, not a second OR clause.
    if position('concept-mapping' in existing_def) > 0 then
        return;
    end if;

    execute 'alter table staging.contract_objects drop constraint contract_objects_kind_identity_check';
    execute format(
        'alter table staging.contract_objects add constraint contract_objects_kind_identity_check '
        'check ((%s) or (contract_kind = %L and contract_id like %L))',
        regexp_replace(existing_def, '^CHECK\s*\((.*)\)$', '\1'),
        'concept-mapping',
        'concept-mapping:%'
    );
end;
$$;
