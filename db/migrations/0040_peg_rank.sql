-- #284 (owner decision 2026-08-17): PEG gets its own ordering, recorded only.
--
-- Deliberately NOT folded into `rank`. `rank` and `target_weight` are what the valuation
-- gap made them, and changing that would move the portfolio before the selection rule is
-- decided. `peg_rank` is module 1's ordering over every decision that has a PEG, lowest
-- first, so a valuation-rejected issuer still shows where its growth-adjusted multiple sits.
--
-- Nullable because PEG is undefined for non-positive earnings or growth: an absent value
-- must not be ranked as though it were the worst.

do $$
begin
    if not exists (
        select 1 from pg_attribute
        where attrelid = 'mart.strategy_decisions'::regclass
          and attname = 'peg_rank' and not attisdropped
    ) then
        alter table mart.strategy_decisions
            add column if not exists peg_rank integer;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.strategy_decisions'::regclass
          and conname = 'strategy_decisions_peg_rank_positive'
          and pg_get_constraintdef(oid) = 'CHECK (((peg_rank IS NULL) OR ((peg IS NOT NULL) AND (peg_rank >= 1))))'
    ) then
        alter table mart.strategy_decisions
            drop constraint if exists strategy_decisions_peg_rank_positive;

        -- A rank only exists where a PEG does, and ranks start at 1.
        alter table mart.strategy_decisions
            add constraint strategy_decisions_peg_rank_positive
            check ((peg_rank is null) or (peg is not null and peg_rank >= 1));
    end if;
end
$$;

comment on column mart.strategy_decisions.peg_rank is
    'Module 1 ordering (#284): position by ascending PEG among decisions that have one. '
    'Independent of `rank` — PEG does not participate in selection.';
