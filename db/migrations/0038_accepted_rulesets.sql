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

-- Extend the contract-kind allowlist for published concept mappings. The allowlist is
-- deliberate: an arbitrary kind would let anything masquerade as a governed contract, so
-- a new kind is a reviewed schema change even though publishing an INSTANCE of it is not.
do $$
begin
    alter table staging.contract_objects
        drop constraint if exists contract_objects_kind_identity_check;
    alter table staging.contract_objects
    add constraint contract_objects_kind_identity_check check (
        (contract_kind = 'registry_snapshot' and contract_id like 'registry-snapshot:%')
        or (contract_kind = 'research_catalog_manifest' and contract_id like 'research-catalog:%')
        or (contract_kind = 'snapshot_manifest' and contract_id like 'snapshot:%')
        or (contract_kind = 'release_manifest' and contract_id like 'release-manifest:%')
        or (contract_kind = 'capture_scope' and contract_id like 'capture-scope:%')
        or (contract_kind = 'capture_manifest' and contract_id like 'capture-manifest:%')
        or (contract_kind = 'capture_evaluation_report' and contract_id like 'capture-evaluation:%')
        or (contract_kind = 'trace_bundle' and contract_id like 'trace-bundle:%')
        or (contract_kind = 'strategy_usage_audit' and contract_id like 'strategy-usage-audit:%')
        or (contract_kind = 'graduation_attestation' and contract_id like 'graduation-attestation:%')
        or (contract_kind = 'concept-mapping' and contract_id like 'concept-mapping:%')
    );
end;
$$;
