-- #837 (module 1, q2): why a decision carries no PEG.
--
-- `factors.base.peg` names every degenerate case — `non_positive_growth`,
-- `insufficient_earnings_history`, `non_positive_earnings`, a missing input — and the
-- strategy evaluator kept only the value. An eligible issuer with no PEG therefore reached
-- `mart.strategy_decisions` as a bare NULL, and the question-coverage report could say only
-- `unrecorded_reason` for it (TSLA, MU, ABBV on the 2026-09-15 staging topt run).
--
-- Not part of the decision's identity: `content_sha256` is computed from the asserted values,
-- and this column annotates them the way the #747 status dimensions do. Folding it into the
-- hash would make every run already persisted raise an identity conflict on replay.
alter table mart.strategy_decisions
    add column if not exists peg_reason_codes text[] not null default '{}'::text[];

comment on column mart.strategy_decisions.peg_reason_codes is
    '#837: the PEG factor''s refusal flags when peg is null for an evaluated issuer; empty when peg is present or the issuer was excluded (see exclusion_reason). Not part of content_sha256.';
