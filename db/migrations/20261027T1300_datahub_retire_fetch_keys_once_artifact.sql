-- #1521 step 6: delete the registry row `fetch-keys-once` from `mart.served_artifact` where a database holds it.
--
-- The row named the schedule `fetch_keys_once_schedule` of the nightly check `fetch_keys_once`. The check is gone:
-- the coverage table of /admin/datahub shows its numbers and gates nothing. No process reads the row. It has
-- no universe pattern, so `mart.served_head` never joined it. No table has a foreign key to it.
--
-- The chain replays on every boot, so this file must be safe to replay.
--   * `20261006T1020` no longer inserts the row. If it did, this delete and that insert would undo each other at
--     each boot.
--   * It acts only when the row exists. The existence check reads the table and takes ACCESS SHARE, so a replay on a
--     settled database never asks for a row lock or for a write lock.
--   * It deletes the one row by its key. It deletes no other row.
--   * The key is retired for good. A later migration must not insert a row with this key again.
do $$
begin
    if exists (select 1 from mart.served_artifact where artifact_key = 'fetch-keys-once') then
        delete from mart.served_artifact where artifact_key = 'fetch-keys-once';
    end if;
end
$$;
