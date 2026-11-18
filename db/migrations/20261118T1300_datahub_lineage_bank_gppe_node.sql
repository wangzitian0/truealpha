-- #1664: mart.capture_issuer_lineage joined only gppe_uniform_charge_v0, so a financial issuer
-- reported the uniform-charge tree sha256, id, version and confidence while its decision used
-- gppe_banking_tce_v1. Join the node the issuer's operating branch decides on.
do $$
declare
    v_old constant text := $o$mnv.node_key = 'gppe_uniform_charge_v0'::text$o$;
    v_new constant text := $n$mnv.node_key = CASE WHEN COALESCE(m.factor_input ->> 'operating_branch'::text, 'non_financial'::text) = 'financial'::text THEN 'gppe_banking_tce_v1'::text ELSE 'gppe_uniform_charge_v0'::text END$n$;
    v_def text;
begin
    if to_regclass('mart.capture_issuer_lineage') is null then
        raise exception 'mart.capture_issuer_lineage is missing';
    end if;
    v_def := pg_get_viewdef('mart.capture_issuer_lineage'::regclass, true);
    if position('gppe_banking_tce_v1' in v_def) > 0 then
        return;
    end if;
    if position(v_old in v_def) = 0 then
        raise exception 'mart.capture_issuer_lineage does not join gppe_uniform_charge_v0 as expected';
    end if;
    execute 'create or replace view mart.capture_issuer_lineage as ' || replace(v_def, v_old, v_new);
end $$;
