-- Seed one contract object per legal kind, so the migration chain can be
-- replayed against a database that has DATA — #615, #617.
--
-- ci-db already applies the chain twice, and the comment on that second pass
-- says why: #455 shipped a `drop constraint` with no IF EXISTS, fine on the
-- first run and fatal on every run after, and it crash-looped llm-service on
-- both environments for days. That gate is real and it works for its case.
--
-- It could not catch #615, because #615 needs rows. `staging.contract_objects`
-- has a closed vocabulary that 0017 creates and 0018, 0038 and 0041 widen.
-- 0017 and 0018 used to rebuild it from a literal list, which threw away the
-- later migrations' clauses and then validated the narrower list against rows
-- that need the wider one. Two passes over an EMPTY database prove the DDL is
-- syntactically re-runnable; they prove nothing about whether it survives the
-- data the system actually produces. One `universe-list:qqq` row was enough to
-- make every llm-service boot abort: 17 restart loops on production, 182 on
-- staging.
--
-- So: run this after the chain, then apply the chain again. Any migration that
-- narrows the vocabulary below what the finished chain allows now fails in CI,
-- on the PR, instead of on the boot after the deploy.

begin;

alter table staging.contract_objects disable trigger trg_contract_objects_append_only;

-- One row per clause of contract_objects_kind_identity_check. The hash is
-- arbitrary but must satisfy the table's own `contract_id ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'`
-- and `content_sha256 ~ '^[0-9a-f]{64}$'` checks.
insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
values
    ('registry-snapshot:' || repeat('a', 64), 'registry_snapshot', repeat('a', 64), '{}'),
    ('research-catalog:' || repeat('b', 64), 'research_catalog_manifest', repeat('b', 64), '{}'),
    ('snapshot:' || repeat('c', 64), 'snapshot_manifest', repeat('c', 64), '{}'),
    ('release-manifest:' || repeat('d', 64), 'release_manifest', repeat('d', 64), '{}'),
    ('capture-scope:' || repeat('e', 64), 'capture_scope', repeat('e', 64), '{}'),
    ('capture-manifest:' || repeat('f', 64), 'capture_manifest', repeat('f', 64), '{}'),
    ('capture-evaluation:' || repeat('0', 64), 'capture_evaluation_report', repeat('0', 64), '{}'),
    ('trace-bundle:' || repeat('1', 64), 'trace_bundle', repeat('1', 64), '{}'),
    ('strategy-usage-audit:' || repeat('2', 64), 'strategy_usage_audit', repeat('2', 64), '{}'),
    ('usage-frequency:' || repeat('3', 64), 'usage_frequency_slice', repeat('3', 64), '{}'),
    ('strategy-data-quality-review:' || repeat('4', 64), 'strategy_data_quality_review', repeat('4', 64), '{}'),
    ('graduation-attestation:' || repeat('5', 64), 'graduation_attestation', repeat('5', 64), '{}'),
    ('concept-mapping:' || repeat('6', 64), 'concept-mapping', repeat('6', 64), '{}'),
    -- The row shape that actually caused #615. `universe_plane` writes the ETF
    -- into the kind and 0041 legitimises it with a LIKE, so the value carries
    -- the ETF rather than being a fixed token.
    ('universe-list:' || repeat('7', 64), 'universe-list:qqq', repeat('7', 64), '{}')
on conflict (contract_id) do nothing;

alter table staging.contract_objects enable trigger trg_contract_objects_append_only;

-- The seed above is a literal list and would rot silently: a migration adding a
-- fifteenth kind would leave it unrepresented, and the replay would pass while
-- covering nothing new. Count the clauses and require one row per clause.
do $$
declare
    clause_count int;
    seeded_count int;
begin
    select count(*) into clause_count
    from regexp_matches(
        (
            select pg_get_constraintdef(constraint_row.oid)
            from pg_constraint as constraint_row
            join pg_class as table_row on table_row.oid = constraint_row.conrelid
            join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
            where schema_row.nspname = 'staging'
              and table_row.relname = 'contract_objects'
              and constraint_row.conname = 'contract_objects_kind_identity_check'
        ),
        'contract_kind',
        'g'
    );

    select count(distinct contract_kind) into seeded_count from staging.contract_objects;

    if seeded_count < clause_count then
        raise exception
            'contract_kind_identity_check has % clauses but only % kinds are seeded; '
            'the replay would pass while covering nothing for the missing ones (#615)',
            clause_count, seeded_count;
    end if;
end;
$$;

commit;
