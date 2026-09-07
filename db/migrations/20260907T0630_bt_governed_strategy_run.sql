-- #575: the strategy run the governed capture head resolves to.
--
-- Both strategy-run twins (Python strategy_run_postgres.py, TypeScript
-- strategy-run-repository.ts) selected `order by executed_at desc limit 1`, which is the
-- bare mutable `latest` CLAUDE.md forbids as a read path: a manual replay at an odd cutoff
-- (2026-09-05 03:10Z on prod) displaced the governed 22:15Z run on every surface for a day,
-- and /research told each visitor it was reading "the governed run every surface resolves".
--
-- The link is the tick's cutoff: lanes/capture.py evaluates the strategy with
-- executed_at = cutoff in the same transaction that freezes the snapshot at that cutoff,
-- then advances the pointer to the capture run. So the governed strategy run is the one
-- whose executed_at equals the cutoff of the head's snapshot, for the head's own universe.
-- A view, executed with the owner's privileges, so mart_readonly can resolve it without
-- reading staging directly (db/roles.sql grants select on mart relations).
create or replace view mart.governed_strategy_run as
select head.environment,
       head.universe_id,
       head.universe_version,
       head.factor_id,
       head.target_run_id,
       head.sequence,
       head.advanced_at,
       s.cutoff,
       r.strategy_run_id,
       r.strategy_key,
       r.executed_at
from mart.current_pointer_head head
join staging.topt_core_snapshots s
  on s.run_id = head.target_run_id
 and s.universe_id = head.universe_id
 and s.universe_version = head.universe_version
join mart.strategy_runs r on r.executed_at = s.cutoff;
