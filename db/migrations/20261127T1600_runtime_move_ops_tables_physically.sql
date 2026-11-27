-- Move operational request queues, verdict/invariant logs, and status views physically into ops schema.
-- Compatibility views remain in staging/mart so internal DB functions and historical rollouts stay intact.
-- Catalog-guarded on relkind so replay under boot lock_timeout takes zero locks.

create schema if not exists ops;

do $$
declare
    rec record;
    src_oid regclass;
    dst_oid regclass;
    src_rk "char";
    dst_rk "char";
begin
    for rec in
        select *
        from (
            values
                ('staging', 'pipeline_trigger_requests'),
                ('staging', 'head_rollback_requests'),
                ('mart', 'nightly_verdicts'),
                ('mart', 'output_invariant_results')
        ) as t(src_schema, rel_name)
    loop
        src_oid := to_regclass(format('%I.%I', rec.src_schema, rec.rel_name));
        dst_oid := to_regclass(format('ops.%I', rec.rel_name));
        select c.relkind into src_rk from pg_class c where c.oid = src_oid;
        select c.relkind into dst_rk from pg_class c where c.oid = dst_oid;

        if src_rk = 'r' then
            if dst_rk = 'v' then
                execute format('drop view ops.%I', rec.rel_name);
            end if;
            if dst_rk is distinct from 'r' then
                execute format('alter table %I.%I set schema ops', rec.src_schema, rec.rel_name);
                execute format('create view %I.%I as select * from ops.%I', rec.src_schema, rec.rel_name, rec.rel_name);
            end if;
        elsif dst_rk = 'r' and src_oid is null then
            execute format('create view %I.%I as select * from ops.%I', rec.src_schema, rec.rel_name, rec.rel_name);
        end if;
    end loop;

    for rec in
        select unnest(array[
            'data_engine_identity',
            'capture_meta_info',
            'capture_status'
        ]) as view_name
    loop
        src_oid := to_regclass(format('mart.%I', rec.view_name));
        dst_oid := to_regclass(format('ops.%I', rec.view_name));
        select c.relkind into src_rk from pg_class c where c.oid = src_oid;
        select c.relkind into dst_rk from pg_class c where c.oid = dst_oid;

        if src_rk = 'v' and dst_oid is null then
            execute format('alter view mart.%I set schema ops', rec.view_name);
            execute format('create view mart.%I as select * from ops.%I', rec.view_name, rec.view_name);
        elsif dst_rk = 'v' and src_oid is null then
            execute format('create view mart.%I as select * from ops.%I', rec.view_name, rec.view_name);
        end if;
    end loop;
end $$;
