-- #1240: drop 7 empty app tables of the access plane that no code reads or writes.
--
-- The chain no longer creates these tables. Databases that ran the old chain still hold them.
-- This file drops them there. The chain replays on every boot, so this file must be safe to replay.
-- The names in the lists below are retired for good. A later migration must not reuse them.
-- A replay drops an empty table, an unused view or an unreferenced function of that name on every boot.
--
-- Rules for each table:
--   * It must exist, and it must hold no row. A table with rows stays, and a WARNING names it.
--     The file does not raise an error for a row. An error would stop every boot and every later file.
--   * Row-level security must not hide rows from the count. The procedure sets row_security = off.
--     A role that row security filters then gets an error, and the table stays.
--   * The file never waits for a lock. LOCK ... NOWAIT fails at once when another session
--     reads or writes the table. The file then skips the table with a WARNING.
--     A table that is always busy shows in the boot log. The next boot tries again.
--   * Dropping a table that has a foreign key takes ACCESS EXCLUSIVE on the referenced table.
--     The referenced tables app.tenants and app.entitlement_grants are live.
--     So the file locks them with NOWAIT before it drops the table.
--   * The file never uses CASCADE. A dependent object makes the drop fail, and a WARNING names it.
--   * An unexpected error becomes a WARNING with its SQLSTATE, and the file goes on.
--     A query cancel, such as a statement timeout, is not caught. It ends the file.
--   * After the objects are gone, the file reads the catalog and locks no table of the app, raw or staging schema.
--
-- The view app.access_audit_metadata reads four of the tables. A table cannot drop while the view exists.
-- The view drops first, and only when each table that it reads holds no row.
-- LOCK on a view also locks every relation that the view reads. So the view lock includes app.principals.
-- The lock uses NOWAIT. The file skips the view with a WARNING when another session reads app.principals.
--
-- Each table runs as its own statement through \gexec. A lock lasts only for that statement.

create or replace procedure pg_temp.retire_unused_view(target text, base_tables text[])
language plpgsql
set search_path = pg_catalog
set row_security = off
as $$
declare
    view_oid oid := to_regclass(target);
    base_table text;
    populated boolean;
begin
    if view_oid is null then
        return;
    end if;
    begin
        execute format('lock table %s in access exclusive mode nowait', target);
        foreach base_table in array base_tables
        loop
            if to_regclass(base_table) is null then
                continue;
            end if;
            execute format('select exists (select 1 from %s)', base_table) into populated;
            if populated then
                raise warning 'retired view % stays: % holds at least 1 row', target, base_table;
                return;
            end if;
        end loop;
        execute format('drop view %s', target);
        raise notice 'retired view % dropped', target;
    exception
        when lock_not_available then
            raise warning 'retired view % is in use; the next boot tries again', target;
        when undefined_table then
            raise notice 'retired view % is gone already', target;
        when others then
            raise warning 'retired view % stays: % (SQLSTATE %)', target, sqlerrm, sqlstate;
    end;
end;
$$;

call pg_temp.retire_unused_view('app.access_audit_metadata', array[
    'app.access_audit_events',
    'app.authorization_decisions',
    'app.authorization_decision_grants',
    'app.publication_policy_sets'
]);

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

-- Child tables come before the tables they reference.
select format('call pg_temp.retire_empty_table(%L)', retired.table_name)
from unnest(array[
    'app.access_audit_events',
    'app.authorization_decision_grants',
    'app.grant_revocations',
    'app.authorization_decisions',
    'app.publication_policy_entitlements',
    'app.publication_policy_set_seals',
    'app.publication_policy_sets'
]) with ordinality as retired(table_name, position)
order by retired.position
\gexec

-- The functions of the retired tables: six trigger functions.
-- The catalog tracks a trigger. A table that stayed keeps its function.
-- The catalog does not track a call inside a function body.
-- So a function also stays while the body of any other function names it, in any letter case.
-- The list holds each caller before the function that it calls.
-- The function app.reject_mutation() is not in the list. The live tables use it.
do $$
declare
    retired_function text;
begin
    foreach retired_function in array array[
        'app.validate_access_audit_decision_tenant()',
        'app.validate_publication_policy_entitlement_insert()',
        'app.validate_publication_policy_set_seal()',
        'app.validate_authorization_decision_policy_set()',
        'app.validate_authorization_decision_grant()',
        'app.validate_authorization_decision_required_grants()'
    ]
    loop
        if to_regprocedure(retired_function) is null then
            continue;
        end if;
        if exists (
            select 1
            from pg_proc as caller
            where caller.oid <> to_regprocedure(retired_function)::oid
              and position(lower(split_part(split_part(retired_function, '(', 1), '.', 2)) in lower(caller.prosrc)) > 0
        ) then
            raise notice 'retired function % stays: the body of another function names it', retired_function;
            continue;
        end if;
        begin
            execute format('drop function %s', retired_function);
            raise notice 'retired function % dropped', retired_function;
        exception
            when others then
                raise notice 'retired function % stays: %', retired_function, sqlerrm;
        end;
    end loop;
end
$$;
