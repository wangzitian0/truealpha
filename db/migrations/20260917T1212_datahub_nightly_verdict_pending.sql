-- release_fetch_proof: a nightly check may record a PENDING verdict (`ok` is null).
--
-- The release fetch proof (apps/data-engine/src/data_engine/quality/release_fetch_proof.py)
-- asks whether the deployment's first scheduled or forced tick fetched from every registered
-- origin. Until that tick has run, the honest answer is neither green nor red. The service's
-- /health reports it as `"ok": null`; tools/nightly_verdicts.py does not page on it, and
-- tools/deploy_freshness.py turns it red once a production release is 26 hours old.
--
-- Replayed on every boot (#916): the column's nullability is read from the catalog first, so
-- a replay takes no lock on the table. The first apply is a catalog-only change.
do $$
begin
    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'mart'
          and table_name = 'nightly_verdicts'
          and column_name = 'ok'
          and is_nullable = 'NO'
    ) then
        alter table mart.nightly_verdicts alter column ok drop not null;
    end if;
end
$$;

do $$
declare
    wanted constant text :=
        'true: the check held; false: it failed; null: pending, the check ran and cannot judge yet '
        '(release_fetch_proof before its first proving tick).';
begin
    if col_description(
        'mart.nightly_verdicts'::regclass,
        (select attnum from pg_attribute where attrelid = 'mart.nightly_verdicts'::regclass and attname = 'ok')
    ) is distinct from wanted then
        execute format('comment on column mart.nightly_verdicts.ok is %L', wanted);
    end if;
end
$$;
