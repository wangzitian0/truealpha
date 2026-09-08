-- 20260908T1027: drop staging.strategy_backtest_inputs.input_key's enumerated CHECK
-- (#770 finding 1, deferred from 0032/0043; init.md rule 22).
--
-- 0032 enumerated the legal `input_key` values in a CHECK so a metric added later
-- (`net_income`, 0043) meant editing this table's constraint before the writer could
-- land it -- a migration to register a metric, exactly what rule 22 forbids ("adding a
-- metric is a registry edit, not a migration"). `truealpha_contracts.metrics.METRICS`
-- (plus the strategy input-key vocabulary aliases next to it) is the source of truth
-- now: `data_engine.datahub.strategy_bridge.seed_strategy_inputs_from_capture` validates
-- `input_key` against the registry before every insert (`is_registered_input_key`), so
-- the same guarantee the CHECK gave -- an unregistered key cannot land in staging --
-- moves from the schema to the writer without moving to a second enumeration.
--
-- Not a rename+recreate: an equivalent CHECK reading the registry is not expressible in
-- SQL without a lookup table this migration would then have to keep in lockstep with the
-- Python registry, recreating the exact problem this drops.

alter table staging.strategy_backtest_inputs
    drop constraint if exists strategy_backtest_inputs_input_key_check;
