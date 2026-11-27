-- Q10 Phase 1: deprecate factor_validation_status without dropping columns yet (rolling-deploy safe).
-- Catalog-guarded via pg_attrdef so replay under boot lock_timeout takes zero locks.

do $$
declare
    tbl text;
    rel regclass;
    has_default boolean;
begin
    foreach tbl in array array[
        'mart.fund_virtual_company',
        'mart.issuer_analyst_ratings',
        'mart.issuer_supply_chain_exposure',
        'mart.issuer_theme_purity',
        'mart.metric_node_values'
    ] loop
        rel := to_regclass(tbl);
        if rel is not null then
            select exists (
                select 1
                from pg_attribute a
                join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
                where a.attrelid = rel
                  and a.attname = 'factor_validation_status'
                  and not a.attisdropped
            ) into has_default;
            if not coalesce(has_default, false) then
                execute format(
                    'alter table %s alter column factor_validation_status set default %L',
                    tbl,
                    'not_evaluated'
                );
            end if;
        end if;
    end loop;
end $$;
