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

-- One row per clause of contract_objects_kind_identity_check.
--
-- The id is derived from the kind rather than written out, because the first
-- version copied runtime_contract.sql's `repeat('5', 64)` style verbatim and
-- collided with that file's own rows on contract_objects_pkey — caught by CI,
-- which is the point of running it. Derived ids also cannot collide with a
-- future contract test that picks the same filler.
--
-- They still satisfy the table's own shape checks:
--   contract_id    ~ '^[a-z][a-z0-9-]*:[0-9a-f]{64}$'
--   content_sha256 ~ '^[0-9a-f]{64}$'
insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload)
select
    seed.id_prefix || ':' || md5(seed.kind) || md5(seed.kind || 'replay-seed'),
    seed.kind,
    md5(seed.kind) || md5(seed.kind || 'replay-seed'),
    '{}'::jsonb
from (
    values
        ('registry-snapshot', 'registry_snapshot'),
        ('research-catalog', 'research_catalog_manifest'),
        ('snapshot', 'snapshot_manifest'),
        ('release-manifest', 'release_manifest'),
        ('capture-scope', 'capture_scope'),
        ('capture-manifest', 'capture_manifest'),
        ('capture-evaluation', 'capture_evaluation_report'),
        ('trace-bundle', 'trace_bundle'),
        ('strategy-usage-audit', 'strategy_usage_audit'),
        ('usage-frequency', 'usage_frequency_slice'),
        ('strategy-data-quality-review', 'strategy_data_quality_review'),
        ('graduation-attestation', 'graduation_attestation'),
        ('concept-mapping', 'concept-mapping'),
        -- The row shape that actually caused #615: `universe_plane` writes the
        -- ETF into the kind and 0041 legitimises it with a LIKE, so the value
        -- carries the ETF rather than being a fixed token. runtime_contract.sql
        -- seeds nine kinds and not this one, which is why it could not have
        -- caught the incident.
        ('universe-list', 'universe-list:qqq')
) as seed(id_prefix, kind)
on conflict (contract_id) do nothing;

alter table staging.contract_objects enable trigger trg_contract_objects_append_only;

-- The seed above is a literal list and would rot silently: a migration adding a
-- fifteenth kind would leave it unrepresented, and the replay would pass while
-- covering nothing new. Count the clauses and require one row per clause.
do $$
declare
    constraint_def text;
    clause_count int;
    seeded_count int;
begin
    select pg_get_constraintdef(constraint_row.oid) into constraint_def
    from pg_constraint as constraint_row
    join pg_class as table_row on table_row.oid = constraint_row.conrelid
    join pg_namespace as schema_row on schema_row.oid = table_row.relnamespace
    where schema_row.nspname = 'staging'
      and table_row.relname = 'contract_objects'
      and constraint_row.conname = 'contract_objects_kind_identity_check';

    -- Fail loudly on a missing or unrecognisable constraint rather than
    -- reporting coverage. Without this, `regexp_matches(NULL, ...)` returns no
    -- rows, clause_count becomes 0, and `seeded_count < 0` can never fire — the
    -- anti-rot check would itself rot silently, which is the exact failure this
    -- file exists to prevent (review).
    if constraint_def is null then
        raise exception
            'contract_objects_kind_identity_check does not exist; the chain did not finish, '
            'and seeding against no vocabulary proves nothing (#615)';
    end if;

    select count(*) into clause_count
    from regexp_matches(constraint_def, 'contract_kind', 'g');

    if clause_count = 0 then
        raise exception
            'contract_objects_kind_identity_check mentions no contract_kind: %. The scan lost '
            'its subject and would pass over anything (#615)', left(constraint_def, 120);
    end if;

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
