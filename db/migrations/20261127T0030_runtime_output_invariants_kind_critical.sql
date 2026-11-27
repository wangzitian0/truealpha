-- Allow 'critical' alongside 'hard' (historical rows) and 'fact' in mart.output_invariant_results.
-- Catalog-guarded on relkind = 'r' and constraint text so replay under boot lock_timeout takes zero locks.

do $$
declare
    rel regclass;
    rk "char";
    cdef text;
begin
    rel := to_regclass('mart.output_invariant_results');
    if rel is not null then
        select c.relkind into rk from pg_class c where c.oid = rel;
        if rk = 'r' then
            select pg_get_constraintdef(c.oid) into cdef
            from pg_constraint c
            where c.conrelid = rel and c.conname = 'output_invariant_results_kind_check';
            if cdef is null or position('critical' in lower(cdef)) = 0 then
                alter table mart.output_invariant_results drop constraint if exists output_invariant_results_kind_check;
                alter table mart.output_invariant_results
                    add constraint output_invariant_results_kind_check check (kind in ('hard', 'fact', 'critical'));
            end if;
        end if;
    end if;
end $$;
