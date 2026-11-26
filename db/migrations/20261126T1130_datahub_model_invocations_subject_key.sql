-- Allow non-SEC LLM invocations (e.g. XHS HTML cards) to log without synthetic integer CIKs (Q08).
-- Every DDL statement is catalog-guarded so replaying under boot lock_timeout takes zero locks.

do $$
declare
    rel oid := to_regclass('staging.model_invocations');
    is_not_null boolean;
    chk_def text;
begin
    if rel is null then
        return;
    end if;

    select a.attnotnull into is_not_null
    from pg_attribute a
    where a.attrelid = rel and a.attname = 'subject_cik' and not a.attisdropped;
    if coalesce(is_not_null, false) then
        alter table staging.model_invocations alter column subject_cik drop not null;
    end if;

    select pg_get_constraintdef(c.oid) into chk_def
    from pg_constraint c
    where c.conrelid = rel and c.conname = 'model_invocations_subject_cik_check';
    if chk_def is not null and position('is null' in lower(chk_def)) = 0 then
        alter table staging.model_invocations drop constraint model_invocations_subject_cik_check;
        alter table staging.model_invocations
            add constraint model_invocations_subject_cik_check
            check ((subject_cik is null) or (subject_cik > 0));
    end if;

    if not exists (
        select 1 from pg_attribute a
        where a.attrelid = rel and a.attname = 'subject_key' and not a.attisdropped
    ) then
        alter table staging.model_invocations add column subject_key text;
    end if;

    if to_regclass('staging.ix_model_invocations_subject_key') is null then
        create index ix_model_invocations_subject_key
            on staging.model_invocations using btree (subject_key, accession, prompt_sha256, model, id desc)
            where (subject_key is not null);
    end if;
end $$;
