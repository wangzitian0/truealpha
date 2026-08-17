-- #284 module 1 (PEG): the two inputs the growth basis needs, and the column the
-- decision records it in.
--
-- `input_key`'s CHECK has been the six keys `large_model_value_v0` consumes since 0032.
-- PEG adds `net_income` (the multiple's denominator, present for 20 of 20 TOPT issuers
-- against 18 for diluted EPS, and the same basis the growth rate is measured on) and
-- `earnings_cagr_3y` (the rate itself, reduced from the annual series by the adapter
-- because company-facts already carries every period — see the note below).
--
-- Recorded but NOT selecting: `mart.strategy_decisions.peg` is nullable and nothing reads
-- it for ranking. Landing module 1 must not move the portfolio before the owner decides
-- how PEG enters selection, and #572's two defects in the multi-period plane are unfixed.
-- Flipping it on is a versioned `definition_version` change, not a migration.
--
-- Why the rate is an input rather than a series: `staging.strategy_backtest_inputs` is
-- keyed (issuer, cutoff, input_key) with no fiscal-period dimension, and giving it one
-- would mean building the vintage-carrying read path that does not exist in Production
-- (#530). The adapter reduces the series where it already holds the whole payload, and
-- the endpoints travel in the observation payload so the window stays auditable.

alter table staging.strategy_backtest_inputs
    drop constraint if exists strategy_backtest_inputs_input_key_check;

alter table staging.strategy_backtest_inputs
    add constraint strategy_backtest_inputs_input_key_check
    check (input_key = any (array[
        'gross_profit',
        'total_assets',
        'headcount',
        'revenue',
        'shares_outstanding',
        'last_close',
        'net_income',
        'earnings_cagr_3y'
    ]));

alter table mart.strategy_decisions
    add column if not exists peg numeric;

-- PEG is only interpretable for positive growth and positive earnings, and the factor
-- returns None rather than a signed value in every degenerate case. A stored non-positive
-- PEG would therefore mean the factor was bypassed, so the database refuses it.
alter table mart.strategy_decisions
    drop constraint if exists strategy_decisions_peg_positive;

alter table mart.strategy_decisions
    add constraint strategy_decisions_peg_positive check (peg is null or peg > 0);

comment on column mart.strategy_decisions.peg is
    'Module 1 PEG (#284): market cap / net income, divided by the annual earnings growth '
    'rate in percentage points. NULL when any input is absent or growth is non-positive. '
    'Recorded only — selection is unchanged until a versioned definition bump enables it.';
