-- #495 (1/3): entity display resolution — the sanctioned "flat 2D projection
-- into mart" (init.md §2.1) that lets consumers render `TICKER · Company`
-- instead of raw LEIs (#494 P0b). One row per issuer on the factor plane:
-- `staging.topt_core_snapshot_members` carries the issuer↔listing linkage for
-- every snapshot member (verified live 2026-07-27: 20 issuers, Alphabet's
-- dual listing collapses to one member per issuer at snapshot level; the
-- 21-listing denominator exists only on the capture plane). Ticker parses
-- from `listing:<mic>:<symbol>`; display_name LEFT-JOINs the knowledge graph
-- and is NULLABLE by design — `staging.kg_entities` is empty today, so
-- consumers must fall back to ticker until KG names land (#496-adjacent).
--
-- Ordinary view (not materialized): the member table is small (one row per
-- issuer per snapshot) and permission checks run against the view owner, so
-- `mart_readonly` reads it without any staging grant — the staging boundary
-- for consumers stays intact.

-- Boot-lock guard: `create or replace view` takes ACCESS EXCLUSIVE on the view, which
-- queues behind every open reader. Replace it only when the definition differs; the
-- comparison normalizes both sides through pg_get_viewdef, so no literal can drift.
do $$
declare
    wanted constant text := $view$
select distinct on (m.issuer_id)
    m.issuer_id,
    m.listing_id,
    upper(split_part(m.listing_id, ':', 3)) as ticker,
    e.display_name
from staging.topt_core_snapshot_members m
left join staging.kg_entities e on e.id = m.issuer_id
order by m.issuer_id, m.created_at desc
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    -- to_regclass, not ::regclass: a cast of a missing name fails when the expression is planned.
    if pg_get_viewdef(to_regclass('mart.entity_display_resolution'))
       is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.entity_display_resolution as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.entity_display_resolution is
    '#495: issuer -> ticker/display_name for consumer rendering. Latest snapshot member per issuer; display_name nullable until the KG carries names.';
