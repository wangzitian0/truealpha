-- #44: Governed prompt-driven Xiaohongshu 1080x1440 HTML card artifacts per strategy run.

-- Every statement runs on every boot. The table and each index are made only when the catalog says
-- they are missing, so a settled database takes no lock.
do $$
begin
    if to_regclass('mart.research_card_artifacts') is null then
        create table mart.research_card_artifacts (
            run_id text not null,
            card_id text not null,
            card_kind text not null,
            subject_key text not null,
            convention text not null,
            universe text not null,
            cutoff_at timestamptz not null,
            card_json jsonb not null,
            html text not null,
            render_mode text not null,
            model text,
            model_invocation_ref text,
            prompt_sha256 text not null,
            recorded_at timestamptz not null default now(),
            constraint research_card_artifacts_pkey primary key (run_id, card_kind, subject_key, convention, universe)
        );
    end if;
    if not exists (
        select 1 from pg_indexes
        where schemaname = 'mart' and indexname = 'idx_mart_research_card_artifacts_card_id'
    ) then
        create index idx_mart_research_card_artifacts_card_id on mart.research_card_artifacts (card_id);
    end if;
end $$;

do $$
begin
    if (
        select d.description
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_description d on d.objoid = c.oid and d.objsubid = 0
        where n.nspname = 'mart' and c.relname = 'research_card_artifacts'
    ) is distinct from
       'Governed 1080x1440 Xiaohongshu HTML card artifacts verified by deterministic fact-preservation guard (#44).' then
        comment on table mart.research_card_artifacts is
            'Governed 1080x1440 Xiaohongshu HTML card artifacts verified by deterministic fact-preservation guard (#44).';
    end if;
end $$;

do $$
begin
    if not exists (
        select 1
        from pg_trigger t
        join pg_class c on c.oid = t.tgrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart'
          and c.relname = 'research_card_artifacts'
          and t.tgname = 'trg_mart_research_card_artifacts_immutable'
    ) then
        create trigger trg_mart_research_card_artifacts_immutable
            before update or delete on mart.research_card_artifacts
            for each row execute function raw.reject_mutation();
    end if;
end $$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'mart_readonly')
       and not has_table_privilege('mart_readonly', 'mart.research_card_artifacts', 'SELECT') then
        grant select on mart.research_card_artifacts to mart_readonly;
    end if;
    if exists (select 1 from pg_roles where rolname = 'app_ops_reader')
       and not has_table_privilege('app_ops_reader', 'mart.research_card_artifacts', 'SELECT') then
        grant select on mart.research_card_artifacts to app_ops_reader;
    end if;
end $$;
