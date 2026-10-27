-- #1522 release N+1: drop `mart.is_head_scheduled(text, text)` where a database holds it.
--
-- The function was a SQL copy of the schedule of the universes. Since release N no process calls it: the output
-- invariants write their schedule predicate from the universe record. The chain no longer creates it (see
-- `20261020T0700`), so a database that ran an older chain is the only holder.
--
-- The chain replays on every boot, so this file must be safe to replay.
--   * It acts only when the function exists. `to_regprocedure` takes no lock, so a replay on a settled database
--     reads the catalog and does nothing.
--   * It never uses CASCADE. No view, default or index depends on the function; a dependent object makes the
--     drop fail, and the boot fails loudly instead of dropping the dependent.
--   * `drop function` locks the function and its catalog row, and no table.
--   * The name is retired for good. A later migration must not create a function of this name again.
do $$
begin
    if to_regprocedure('mart.is_head_scheduled(text,text)') is not null then
        drop function mart.is_head_scheduled(text, text);
    end if;
end
$$;
