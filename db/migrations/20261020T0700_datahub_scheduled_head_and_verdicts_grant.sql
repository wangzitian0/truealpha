-- #1346 / #1295: operator read grant on mart.nightly_verdicts so /admin/datahub can display in-environment
-- check verdicts.
--
-- This file used to create `mart.is_head_scheduled(text, text)`, a SQL copy of the schedule of the universes
-- for the output invariants. Release N of #1522 moved every reader to the predicate that
-- `output_invariants.head_is_scheduled_sql` writes from the universe record. This release (N+1) removes the
-- function from this file and drops it from databases that hold it, in
-- `20261027T1200_datahub_retire_head_scheduled_function.sql`. The chain replays on every boot, so this file must
-- not create the function again: the drop file would then churn at each boot.

-- Grant app_ops_reader SELECT on mart.nightly_verdicts (#876) so /admin/datahub renders
-- the in-environment check verdicts directly. Guarded by has_table_privilege so a replay
-- on a settled database takes no lock.
do $$
begin
    if exists (select from pg_roles where rolname = 'app_ops_reader')
       and to_regclass('mart.nightly_verdicts') is not null
       and not has_table_privilege('app_ops_reader', 'mart.nightly_verdicts', 'SELECT')
    then
        grant select on mart.nightly_verdicts to app_ops_reader;
    end if;
end;
$$;
