-- #877 PR-3: capture side-table for external entity references (docs/entity-identity.md §5).
--
-- The normalized payload carries only opaque UUIDs (issuer_id, instrument_id, listing_id).
-- The aliases a capture used (e.g. CIK sent to SEC, ticker sent to Twelve Data) are lineage
-- and stay out of the payload, recorded here in staging.capture_entity_refs.
--
-- This side table answers "which external identifier did we send to which vendor?",
-- making vendor calls reproducible without polluting the payload hash or breaking cross-universe reuse.

do $$
begin
    if to_regclass('staging.capture_entity_refs') is null then
        create table staging.capture_entity_refs (
            observation_id  text not null references staging.capture_normalized_observations(observation_id),
            role            text not null check (role in ('issuer', 'instrument', 'listing')),
            entity_id       uuid not null references staging.entities(entity_id),
            scheme          text not null references staging.entity_alias_schemes(scheme),
            value           text not null,
            known_at        timestamptz not null,
            recorded_at     timestamptz not null default clock_timestamp(),
            primary key (observation_id, role)
        );
    end if;
end
$$;

do $$
begin
    if to_regclass('staging.idx_capture_entity_refs_entity') is null then
        create index if not exists idx_capture_entity_refs_entity on staging.capture_entity_refs (entity_id);
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgrelid = to_regclass('staging.capture_entity_refs')
          and not tgisinternal
          and pg_get_triggerdef(oid) = 'CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON staging.capture_entity_refs FOR EACH ROW EXECUTE FUNCTION staging.reject_entity_mutation()'
    ) then
        drop trigger if exists reject_mutation on staging.capture_entity_refs;
        create trigger reject_mutation
        before update or delete on staging.capture_entity_refs
        for each row execute function staging.reject_entity_mutation();
    end if;
end
$$;
