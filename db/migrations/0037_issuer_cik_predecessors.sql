-- #496: issuer CIK predecessor registry — the owner-signed registry metadata
-- the predecessor-CIK fallback (#519) resolves from. The architecture red line
-- forbids ticker allowlists BAKED INTO CAPTURE CODE; issuer facts belong in
-- versioned registry data, which is exactly what this table is: every row
-- carries its reason and approver, lands via a reviewed migration, and is
-- append-only history. The fallback self-disables the day the successor CIK
-- publishes a real taxonomy, at which point the row is inert lineage.

create table if not exists staging.issuer_cik_predecessors (
    issuer_id       text primary key check (issuer_id like 'issuer:%'),
    predecessor_cik bigint not null check (predecessor_cik > 0),
    reason          text not null check (length(reason) > 0),
    approved_by     text not null check (length(approved_by) > 0),
    recorded_at     timestamptz not null default clock_timestamp()
);

comment on table staging.issuer_cik_predecessors is
    '#496: owner-approved predecessor CIKs consulted only when the SEC-index-mapped CIK yields an empty taxonomy.';

create or replace function staging.reject_cik_predecessor_mutation()
returns trigger language plpgsql as $$
begin
    raise check_violation using message = 'issuer_cik_predecessors is append-only registry history';
end;
$$;

drop trigger if exists reject_mutation on staging.issuer_cik_predecessors;
create trigger reject_mutation
before update or delete on staging.issuer_cik_predecessors
for each row execute function staging.reject_cik_predecessor_mutation();

-- Seed: ExxonMobil. SEC's ticker index repointed XOM to the post-reorganization
-- holdco CIK 2115436 (zero us-gaap concepts, formerNames []); consolidated
-- history lives in CIK 34088 and no SEC metadata links the two.
insert into staging.issuer_cik_predecessors (issuer_id, predecessor_cik, reason, approved_by)
values (
    'issuer:lei:J3WHBG0MTS7O8ZVMDC91',
    34088,
    '2026 ExxonMobil holdco reorganization: index CIK 2115436 publishes no us-gaap taxonomy yet; no machine-readable predecessor pointer exists in SEC metadata',
    'zitian (owner decision 2026-07-29, truealpha#496)'
)
on conflict (issuer_id) do nothing;
