-- #1061: drop 15 empty tables that no code reads or writes.
--
-- The chain no longer creates these tables. Databases that ran the old chain still hold them.
-- This file drops them there. The chain replays on every boot, so this file must be safe to replay.
-- The names in the two lists below are retired for good. A later migration must not reuse them.
-- A replay drops an empty table or an unreferenced function of that name on every boot.
--
-- Rules for each table:
--   * It must exist, and it must hold no row. A table with rows stays, and a WARNING names it.
--   * Row-level security must not hide rows from the count. The procedure sets row_security = off.
--     A role that row security filters then gets an error, and the table stays.
--   * The file never waits for a lock. LOCK ... NOWAIT fails at once when another session
--     reads or writes the table. The file then skips the table with a WARNING.
--     A table that is always busy shows in the boot log. The next boot tries again.
--   * Dropping a table that has a foreign key takes ACCESS EXCLUSIVE on the referenced table.
--     The referenced tables app.tenants, app.principals and raw.capture_runs are live.
--     So the file locks them with NOWAIT before it drops the table.
--   * The file never uses CASCADE. A dependent object makes the drop fail, and a WARNING names it.
--   * An unexpected error becomes a WARNING with its SQLSTATE, and the file goes on.
--     A query cancel, such as a statement timeout, is not caught. It ends the file.
--   * After the tables are gone, the file reads the catalog and locks no table of the app, raw or staging schema.
--
-- Each table runs as its own statement through \gexec. A lock lasts only for that statement.

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
    'staging.headcount_facts',
    'staging.headcount_extraction_invocations',
    'staging.filing_documents',
    'staging.mvp_market_prices',
    'staging.mvp_financial_facts',
    'staging.mvp_corporate_actions',
    'staging.mvp_universe_memberships',
    'staging.mvp_issuer_security_links',
    'staging.mvp_security_listing_links',
    'staging.normalized_records',
    'raw.capture_checkpoints',
    'raw.recapture_plans',
    'app.tenant_memberships',
    'app.publication_policies',
    'app.private_research_objects'
]) with ordinality as retired(table_name, position)
order by retired.position
\gexec

-- The functions of the retired tables: trigger functions and two check helpers.
-- The catalog tracks a trigger or a check constraint. A table that stayed keeps its function.
-- The catalog does not track a call inside a function body.
-- So a function also stays while the body of any other function names it, in any letter case.
-- The list holds each caller before the function that it calls.
do $$
declare
    retired_function text;
begin
    foreach retired_function in array array[
        'staging.validate_normalized_raw_lineage()',
        'staging.validate_filing_document_projection()',
        'staging.validate_normalized_restatement()',
        'staging.validate_headcount_invocation()',
        'staging.validate_headcount_projection()',
        'staging.validate_mvp_projection()',
        'raw.validate_capture_checkpoint_address()',
        'raw.validate_recapture_plan_address()',
        'raw.enforce_capture_checkpoint_progress()',
        'raw.validate_checkpoint_obligation_refs()',
        'raw.validate_recapture_obligation_refs()',
        'raw.has_canonical_obligation_ids(text[], boolean)',
        'raw.has_canonical_text_json_array(jsonb, boolean)'
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
