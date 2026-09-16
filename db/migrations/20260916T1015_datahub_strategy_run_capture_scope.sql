-- #877 PR-1: a strategy run names the capture run it was evaluated for.
--
-- Until now the only link between a strategy run and a capture run was the cutoff:
-- mart.governed_strategy_run joined `strategy_runs.executed_at = capture cutoff`, and both
-- strategy-run twins joined `strategy_decisions` to `mart.topt_core_results` on
-- `(issuer_id, cutoff)` with no run at all. A cutoff is not a run. Since #874 a forced
-- TOPT tick captures again at the SAME executed_at as the scheduled tick, so one cutoff
-- carries two TOPT capture runs, two sets of core results and (when the inputs moved)
-- two strategy runs:
--   * the twins returned every decision twice, once per capture run's core result;
--   * the view marked both strategy runs governed, and the twins then served whichever was
--     created last, even when the forced run's pointer advance was withheld.
-- Once TOPT and the plane universes share issuer ids (#877 PR-4), a QQQ or canary run at
-- the same cutoff would do the same.
--
-- The tick now records the link (`lanes/capture.py` -> `strategy_bridge`), and readers
-- resolve the run through it. Append-only, like the rows it links: one capture run may
-- bind more than one strategy run only if it is re-published, and one strategy run may be
-- bound by two capture runs when a forced capture reproduced identical inputs (the run id
-- is content-addressed).

create table if not exists mart.strategy_run_capture_bindings (
    capture_run_id   text not null references raw.capture_runs (run_id),
    strategy_run_id  text not null references mart.strategy_runs (strategy_run_id),
    -- 'tick': recorded by the tick that evaluated the strategy for this capture.
    -- 'cutoff-backfill': inferred below for history, and only where the cutoff was
    -- unambiguous. The two stay distinguishable so the inference is never mistaken for a
    -- record.
    bound_by         text not null check (bound_by in ('tick', 'cutoff-backfill')),
    bound_at         timestamptz not null default clock_timestamp(),
    primary key (capture_run_id, strategy_run_id)
);

create index if not exists idx_strategy_run_capture_bindings_strategy_run
    on mart.strategy_run_capture_bindings (strategy_run_id);

comment on table mart.strategy_run_capture_bindings is
    '#877: which capture run a strategy run was evaluated for. Readers scope core results '
    'and the governed strategy run through this link, never through the cutoff alone.';

drop trigger if exists trg_strategy_run_capture_bindings_append_only on mart.strategy_run_capture_bindings;
create trigger trg_strategy_run_capture_bindings_append_only
before update or delete on mart.strategy_run_capture_bindings
for each row execute function mart.reject_mutation();

-- History: every strategy run recorded before the tick wrote bindings is bound to the TOPT
-- capture run at its executed_at, but ONLY when that cutoff has exactly one materialized
-- TOPT capture run and exactly one strategy run. An ambiguous cutoff stays unbound: a
-- guess there is the defect this migration removes. Measured 2026-09-16 before writing
-- this: production 65/65 and staging 250/250 strategy runs match uniquely, and neither
-- environment has recorded a forced run yet.
--
-- Migrations re-apply on every boot, so the inference is fenced to runs created before
-- the first tick-recorded binding: after that, an unbound strategy run is a replay the
-- tick never evaluated, and the cutoff must not bind it.
insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by, bound_at)
select snapshot.run_id, run.strategy_run_id, 'cutoff-backfill', run.created_at
from mart.strategy_runs run
join staging.topt_core_snapshots snapshot
  on snapshot.cutoff = run.executed_at
 and snapshot.universe_id like 'universe:topt-%'
where run.created_at < coalesce(
        (select min(recorded.bound_at) from mart.strategy_run_capture_bindings recorded
         where recorded.bound_by = 'tick'),
        'infinity'::timestamptz)
  and not exists (
        select 1 from mart.strategy_run_capture_bindings bound
        where bound.strategy_run_id = run.strategy_run_id)
  and (select count(*) from staging.topt_core_snapshots other
       where other.cutoff = run.executed_at and other.universe_id like 'universe:topt-%') = 1
  and (select count(*) from mart.strategy_runs other
       where other.executed_at = run.executed_at) = 1
on conflict do nothing;

-- #575's view, same columns, resolved through the binding instead of the cutoff. The head
-- is unchanged: production, gross_profit_per_employee, the served TOPT universe, the
-- newest advance. Per strategy key the view yields at most one run, the latest bound to
-- the head's capture run, so a forced run that advanced the pointer resolves its own
-- strategy run and a forced run that was withheld resolves nothing.
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
join lateral (
    select distinct on (candidate.strategy_key)
           candidate.strategy_run_id, candidate.strategy_key, candidate.executed_at
    from mart.strategy_run_capture_bindings binding
    join mart.strategy_runs candidate on candidate.strategy_run_id = binding.strategy_run_id
    where binding.capture_run_id = head.target_run_id
    order by candidate.strategy_key, binding.bound_at desc, candidate.strategy_run_id desc
) run on true;

-- The one capture run a strategy run's decisions are read against: the governed head's,
-- when the governed head resolves this strategy run; otherwise the latest binding. Both
-- strategy-run twins join core results through this view (`strategy-run-repository.ts`,
-- `strategy_run_postgres.py`), so a decision meets exactly one core result.
create or replace view mart.strategy_run_capture as
select distinct on (binding.strategy_run_id)
       binding.strategy_run_id,
       binding.capture_run_id,
       (governed.strategy_run_id is not null) as governed
from mart.strategy_run_capture_bindings binding
left join mart.governed_strategy_run governed
  on governed.strategy_run_id = binding.strategy_run_id
 and governed.target_run_id = binding.capture_run_id
order by binding.strategy_run_id,
         (governed.strategy_run_id is not null) desc,
         binding.bound_at desc,
         binding.capture_run_id desc;

comment on view mart.strategy_run_capture is
    '#877: the capture run a strategy run''s decisions are read against (the governed head''s '
    'when it resolves the run, else the latest binding).';

-- mart_readonly receives select on new mart relations from db/roles.sql, which runs after
-- migrations.
