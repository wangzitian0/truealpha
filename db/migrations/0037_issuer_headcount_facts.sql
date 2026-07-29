-- #70 / #496: employee headcount as an append-only, point-in-time fact table.
--
-- Headcount is the DENOMINATOR of gross-profit-per-employee, and until now it was a
-- 21-entry dict literal in `datahub/production_topt/headcount.py`. That is not a mapping
-- problem the way XBRL tag variance is: SEC company-facts publishes no employee-count
-- concept at all (verified across AAPL/MSFT/JPM — every "Employee*" concept there is
-- share-based-compensation), so the figure exists only as prose in a 10-K. It has to be
-- extracted, and extraction needs judgement.
--
-- Judgement belongs on a slower plane than the daily tick. This table is the seam: many
-- LOW-frequency producers may write to it (a reviewed manual entry today, #70's text
-- extraction later, a vendor feed) and the HIGH-frequency tick only reads. Adding an
-- issuer, or correcting a figure, is an insert here — never a code change and never a
-- deploy.
--
-- Append-only with an explicit `knowable_at`: a correction supersedes by landing a newer
-- row, so a point-in-time read at an old cutoff still sees what was knowable then. That
-- is also why the tick must select by knowable_at <= cutoff rather than "the latest row".

create table if not exists staging.issuer_headcount_facts (
    id              bigint generated always as identity primary key,
    cik             integer not null check (cik > 0),
    headcount       numeric not null check (headcount > 0),
    -- When this figure became knowable — the filing date for an extracted fact, the
    -- disclosure date for a reviewed one. Never an insertion clock: a fact stamped at
    -- insert time would silently become look-ahead for any historical cutoff.
    knowable_at     timestamptz not null,
    -- The period the headcount describes, when the source states one. Null is honest for
    -- a cover-page "as of" figure that names no period.
    period_end      date,
    source          text not null check (source <> ''),
    -- Free-form pointer at what justifies the number: an accession + item reference, a
    -- raw.fetches id, a #70 evidence span. Required, because a headcount with no stated
    -- justification is indistinguishable from a guess.
    evidence_ref    text not null check (evidence_ref <> ''),
    confidence      numeric not null check (confidence >= 0 and confidence <= 1),
    recorded_at     timestamptz not null default now(),
    -- Ingestion audit time only; PIT reads use knowable_at.
    check (recorded_at >= knowable_at - interval '400 days')
);

comment on table staging.issuer_headcount_facts is
    '#70: append-only PIT employee headcount. Many low-frequency producers write; the daily capture only reads by knowable_at <= cutoff. Corrections supersede by insert, never by update.';

create index if not exists idx_issuer_headcount_pit
    on staging.issuer_headcount_facts (cik, knowable_at desc);

drop trigger if exists reject_mutation on staging.issuer_headcount_facts;
create trigger reject_mutation
before update or delete on staging.issuer_headcount_facts
for each row execute function raw.reject_mutation();
