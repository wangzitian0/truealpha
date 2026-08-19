-- 0043: give the strategy input transport a fiscal-period axis (#284, #530-adjacent).
--
-- `staging.strategy_backtest_inputs` was keyed (issuer, cutoff, input_key) with no
-- dimension for the period a value DESCRIBES. One value per key per cutoff, so a factor
-- needing history could not be expressed here at all — and module 1 needs three years of
-- net income. The consequence was not a missing feature but a misplaced one: the growth
-- rate was reduced inside `sec_financial_adapter` and shipped as the scalar
-- `earnings_cagr_3y`, which is factor arithmetic executing in the capture layer, exactly
-- what init.md rule 2 and the AGENTS.md red line forbid. The transport was too narrow to
-- express the alternative, so the rule lost.
--
-- With a period axis the series crosses whole and `factors.base.peg` reduces it. That
-- deletes `_earnings_cagr` from the adapter, deletes the `earnings_cagr_3y` input key for
-- new runs, and collapses module 1's two entry points into one.
--
-- NULL means "this value is not about a fiscal period" — a price, a share count, a
-- headcount. Those are point-in-time observations at the cutoff and always were; giving
-- them a synthetic period would be the same lie as a synthetic `knowable_at`.

alter table staging.strategy_backtest_inputs
    add column if not exists fiscal_period text;

comment on column staging.strategy_backtest_inputs.fiscal_period is
    'The fiscal period this value describes, as the staging period tag '
    '"<filing FY>:<kind>:<start>:<end>". NULL for point-in-time observations (price, '
    'share count, headcount) which describe an instant rather than a period. A metric '
    'may appear once per period per cutoff: that is the axis that lets a multi-period '
    'factor live in libs/factors instead of being pre-reduced in the capture layer.';

-- One value per (issuer, cutoff, key, period, vintage). The pre-0043 constraint had no
-- period column, so a second annual period would have collided with the first at the same
-- `recorded_at`. Rows written before this migration carry NULL and stay unique on the
-- same tuple, so no historical row changes meaning.
-- Postgres truncated the implicit name to 63 characters, so it is spelled out rather than
-- reconstructed from the column list -- getting it wrong leaves the old constraint in
-- place, which silently rejects the second period of every series.
alter table staging.strategy_backtest_inputs
    drop constraint if exists strategy_backtest_inputs_issuer_id_cutoff_at_input_key_reco_key;

create unique index if not exists strategy_backtest_inputs_identity
    on staging.strategy_backtest_inputs
    (issuer_id, cutoff_at, input_key, coalesce(fiscal_period, ''), recorded_at);

-- The as-of read path selects the newest vintage per (issuer, key, period).
create index if not exists idx_strategy_backtest_inputs_period
    on staging.strategy_backtest_inputs (cutoff_at, issuer_id, input_key, fiscal_period, recorded_at desc);
