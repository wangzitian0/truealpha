-- idempotent: true
-- Drop two composite indexes on staging.evidence_edges that no reader can use (#1699).
-- The readers filter on from_id or to_id alone; idx_evidence_edges_from_id and
-- idx_evidence_edges_to_id serve them. from_kind and to_kind add no selectivity.
-- Each drop runs only while the index exists, so a replay takes no lock.
do $$ begin if to_regclass('staging.evidence_edges_from_idx') is not null then
drop index staging.evidence_edges_from_idx;
end if; end $$;
do $$ begin if to_regclass('staging.evidence_edges_to_idx') is not null then
drop index staging.evidence_edges_to_idx;
end if; end $$;
