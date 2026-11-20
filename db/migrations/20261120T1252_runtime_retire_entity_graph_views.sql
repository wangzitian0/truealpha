-- idempotent: true
-- Retire three staging views that no code, view or function reads (#1699).
-- staging.entity_graph_nodes reads staging.entity_alias_resolution, so it drops first.
-- Each drop runs only while the view exists, so a replay takes no lock.
do $$ begin if to_regclass('staging.entity_graph_nodes') is not null then
drop view staging.entity_graph_nodes;
end if; end $$;
do $$ begin if to_regclass('staging.entity_graph_edges') is not null then
drop view staging.entity_graph_edges;
end if; end $$;
do $$ begin if to_regclass('staging.entity_alias_resolution') is not null then
drop view staging.entity_alias_resolution;
end if; end $$;
