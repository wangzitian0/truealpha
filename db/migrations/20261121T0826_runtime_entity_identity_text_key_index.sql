-- idempotent: true
-- mart keys carry issuer and listing ids as text. Readers join mart.entity_identity on
-- entity_id::text, which the uuid index uq_mart_entity_identity cannot serve. Each such
-- lookup scanned the whole view: on staging the wide panel read took 9.2 s for 5,692
-- rows and the 5 s statement_timeout failed /research/rankings and /research/coverage.
-- This index serves every `entity_id::text = ...` lookup (#1699).
do $$ begin if to_regclass('mart.ix_mart_entity_identity_entity_text') is null then
create index ix_mart_entity_identity_entity_text on mart.entity_identity using btree ((entity_id::text));
end if; end $$;
