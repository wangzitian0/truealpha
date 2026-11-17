-- #1664: Add 'not_applicable' to AvailabilityStatus check constraints on TOPT and strategy tables.
-- Boot-lock guard: check pg_get_constraintdef first so replay on every boot skips ALTER TABLE when already applied.

do $$
declare
    tbl text;
    cname text;
begin
    foreach tbl in array array['mart.topt_gppe_results', 'mart.topt_core_results', 'mart.strategy_decisions'] loop
        if not exists (
            select 1
            from pg_constraint con
            join pg_attribute att
              on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
            where con.conrelid = tbl::regclass
              and con.contype = 'c'
              and att.attname = 'availability_status'
              and not att.attisdropped
              and pg_get_constraintdef(con.oid) like '%not_applicable%'
        ) then
            select con.conname into cname
            from pg_constraint con
            join pg_attribute att
              on att.attrelid = con.conrelid and att.attnum = any(con.conkey)
            where con.conrelid = tbl::regclass
              and con.contype = 'c'
              and att.attname = 'availability_status'
              and not att.attisdropped
            limit 1;
            if cname is not null then
                execute format('alter table %s drop constraint %I', tbl, cname);
            end if;
            execute format(
                'alter table %s add constraint %I check (availability_status in (''available'', ''unavailable'', ''stale'', ''excluded'', ''low_confidence'', ''not_applicable'', ''error''))',
                tbl,
                split_part(tbl, '.', 2) || '_availability_status_check'
            );
        end if;
    end loop;
end $$;
