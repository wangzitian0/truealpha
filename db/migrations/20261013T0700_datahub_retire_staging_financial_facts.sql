-- #1258: drop empty staging.financial_facts that no code reads or writes.
--
-- The chain no longer creates this table. Databases that ran the old chain still hold it.
-- This file drops it there. The chain replays on every boot, so this file must be safe to replay.
-- The name is retired for good. A later migration must not reuse it.
-- A replay drops the empty table on every boot.
--
-- Rules for the table:
--   * It must exist, and it must hold no row. A table with rows stays, and a WARNING names it.
--     The file does not raise an error for a row. An error would stop every boot and every later file.
--   * Row-level security must not hide rows from the count. The procedure sets row_security = off.
--   * The file never waits for a lock. LOCK ... NOWAIT fails at once when another session
--     reads or writes the table. The file then skips the table with a WARNING.
--     A table that is always busy shows in the boot log. The next boot tries again.
--   * Dropping a table that has a foreign key takes ACCESS EXCLUSIVE on the referenced table.
--     staging.financial_facts references staging.kg_entities.
--     So the file locks staging.kg_entities with NOWAIT before it drops the table.
--   * The file never uses CASCADE. A dependent object makes the drop fail, and a WARNING names it.
--   * An unexpected error becomes a WARNING with its SQLSTATE, and the file goes on.
--     A query cancel, such as a statement timeout, is not caught. It ends the file.
--   * After the table is gone, the file reads the catalog and locks no table.

create or replace procedure pg_temp.retire_empty_table(target text)
language plpgsql
set search_path = pg_catalog
set row_security = off
as $$
declare
    table_oid oid := to_regclass(target);
    parent text;
    sampled bigint;
begin
    if table_oid is null then
        return;
    end if;
    begin
        for parent in
            select distinct confrelid::regclass::text
            from pg_constraint
            where conrelid = table_oid
              and contype = 'f'
              and confrelid <> table_oid
            order by 1
        loop
            execute format('lock table %s in access exclusive mode nowait', parent);
        end loop;
        execute format('lock table %s in access exclusive mode nowait', target);
        execute format('select count(*) from (select 1 from %s limit 1000) as sample', target) into sampled;
        if sampled = 0 then
            execute format('drop table %s', target);
            raise notice 'retired table % dropped', target;
        else
            raise warning 'retired table % holds at least % row(s); it stays', target, sampled;
        end if;
    exception
        when lock_not_available then
            raise warning 'retired table % is in use; the next boot tries again', target;
        when undefined_table then
            raise notice 'retired table % is gone already', target;
        when others then
            raise warning 'retired table % stays: % (SQLSTATE %)', target, sqlerrm, sqlstate;
    end;
end;
$$;

call pg_temp.retire_empty_table('staging.financial_facts');
