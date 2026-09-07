-- #575: the strategy run the governed capture head resolves to.
--
-- Both strategy-run twins (Python strategy_run_postgres.py, TypeScript
-- strategy-run-repository.ts) selected `order by executed_at desc limit 1`, which is the
-- bare mutable `latest` CLAUDE.md forbids as a read path: a manual replay at an odd cutoff
-- (2026-09-05 03:10Z on prod) displaced the governed 22:15Z run on every surface for a day,
-- and /research told each visitor it was reading "the governed run every surface resolves".
--
-- "Governed" means ONE head: the same key the App's TOPT reads use (topt-gppe-repository.ts
-- POINTER_HEAD_SQL) — production, gross_profit_per_employee, the served TOPT universe, the
-- newest advance. Expanding every head would let the canary or QQQ pointer mark a run as
-- governed (review on #755). The link to the run is the tick's cutoff: lanes/capture.py
-- evaluates the strategy with executed_at = cutoff in the same transaction that captures
-- at that cutoff, then advances the pointer to the capture run; mart.topt_capture_status
-- already exposes that run's campaign cutoff in mart.
-- A view, executed with the owner's privileges, so mart_readonly can resolve it without
-- reading raw directly (db/roles.sql grants select on mart relations).
create or replace view mart.governed_strategy_run as
with head as (
    select environment, universe_id, universe_version, factor_id, target_run_id, sequence, advanced_at
    from mart.current_pointer_head
    where environment = 'production'
      and factor_id = 'gross_profit_per_employee'
      and universe_id like 'universe:topt-%'
    order by advanced_at desc
    limit 1
)
select head.environment,
       head.universe_id,
       head.universe_version,
       head.factor_id,
       head.target_run_id,
       head.sequence,
       head.advanced_at,
       status.cutoff,
       run.strategy_run_id,
       run.strategy_key,
       run.executed_at
from head
join mart.topt_capture_status status on status.run_id = head.target_run_id
join mart.strategy_runs run on run.executed_at = status.cutoff;
