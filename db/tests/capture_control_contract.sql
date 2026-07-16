\set ON_ERROR_STOP on

begin;

do $$ begin
    if raw.canonical_timestamp('2026-04-01T00:00:00.123000Z')
       <> '2026-04-01T00:00:00.123000Z' then
        raise exception 'Postgres timestamp JSON differs from Pydantic JSON';
    end if;
    if raw.canonical_json('{"z":1,"A":2,"ä":3,"_":4}')
       <> '{"A":2,"_":4,"z":1,"\u00e4":3}' then
        raise exception 'Postgres key ordering or Unicode escaping differs from Python JSON';
    end if;
    if raw.has_canonical_subjects('[{"kind":"company","id":"company:goog"}]')
       or raw.has_canonical_subjects('[{"kind":"listing","id":"listing:latest"}]') then
        raise exception 'subject validator accepted a non-contract kind or mutable ID';
    end if;
    if raw.has_canonical_universe_refs(
        '[{"universe_id":"universe:current","universe_version":"v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'
    ) then
        raise exception 'universe validator accepted a mutable reference';
    end if;
    if raw.has_retry_outcome_partition(
        array['rate_limited', 'interrupted'],
        array['failed', 'success', 'unavailable', 'unchanged']
    ) or raw.has_retry_outcome_partition(
        array['interrupted', 'rate_limited'],
        array['failed', 'interrupted', 'success', 'unavailable', 'unchanged']
    ) then
        raise exception 'retry partition accepted noncanonical or overlapping outcomes';
    end if;
end $$;

do $$
declare
    candidate record;
    payload jsonb;
begin
    for candidate in
        select * from (values
            ('capture-policy:main', 'github_ci', 'mutable campaign policy'),
            ('capture-policy:d5:v1', 'invalid_env', 'unknown campaign environment')
        ) as candidates(policy_id, environment, description)
    loop
        payload := jsonb_build_object(
            'campaign_policy_id', candidate.policy_id,
            'environment', candidate.environment,
            'cutoff', '2026-04-01T00:00:00Z',
            'universe_refs', '[{"universe_id":"universe:topt-us-2026-03-31","universe_version":"topt-sql-contract-v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'::jsonb
        );
        begin
            insert into raw.capture_campaigns (
                campaign_id, content_sha256, policy_id, environment, cutoff, universe_refs
            ) values (
                'capture-campaign:' || raw.canonical_sha256(
                    jsonb_build_object('kind', 'capture-campaign', 'identity', payload)
                ),
                raw.canonical_sha256(payload), candidate.policy_id, candidate.environment,
                '2026-04-01T00:00:00Z', payload->'universe_refs'
            );
            raise exception '% unexpectedly succeeded', candidate.description;
        exception when check_violation then null;
        end;
    end loop;
end $$;

do $$
declare
    payload jsonb;
begin
    payload := jsonb_build_object(
        'universe', jsonb_build_object(
            'universe_id', 'universe:current',
            'universe_version', 'topt-sql-contract-v1',
            'content_sha256', repeat('8', 64)
        ),
        'members', '[{"kind":"listing","id":"listing:xnas:goog"}]'::jsonb,
        'effective_at', '2026-04-01T00:00:00Z'
    );
    begin
        insert into raw.capture_list_versions (
            list_version_id, universe_id, universe_version, universe_sha256,
            effective_at, member_count, members, content_sha256
        ) values (
            'list-version:' || raw.canonical_sha256(payload),
            'universe:current', 'topt-sql-contract-v1', repeat('8', 64),
            '2026-04-01T00:00:00Z', 1, payload->'members', raw.canonical_sha256(payload)
        );
        raise exception 'mutable list universe unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_list_versions (
    list_version_id, universe_id, universe_version, universe_sha256,
    effective_at, effective_at_canonical, member_count, members, content_sha256
) values (
    'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef',
    'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
    '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00', 1,
    '[{"kind":"listing","id":"listing:xnas:goog"}]',
    '503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef'
);
insert into raw.capture_list_version_members (
    list_version_id, member_ordinal, subject_kind, subject_id
) values (
    'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef',
    1, 'listing', 'listing:xnas:goog'
);

-- One complete +08:00 chain proves every timestamp-address trigger preserves
-- the original Pydantic representation rather than reconstructing UTC text.
insert into raw.capture_campaigns (
    campaign_id, content_sha256, policy_id, environment, cutoff, cutoff_canonical, universe_refs
) values (
    'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
    '5bbc9eff88ea81db2c83c97817fc9f4696a11223c443b1cd2513e88a6a88f38b',
    'capture-policy:d5-offset:v1', 'github_ci',
    '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00',
    '[{"universe_id":"universe:topt-us-2026-03-31","universe_version":"topt-sql-contract-v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'
);
insert into raw.capture_schedule_policies (
    schedule_policy_id, content_sha256, policy_version, demanded_cadence,
    provider_availability_cadence, freshness_max_age, retry_policy, payload
) values (
    'schedule-policy:6666666666666666666666666666666666666666666666666666666666666666',
    repeat('6', 64), 'sql-contract:v1', interval '1 day',
    'fixture-daily:v1', interval '2 days', '{}'::jsonb, '{}'::jsonb
);
insert into raw.capture_runs (
    run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256
) values (
    'capture-run:97af27541be16406f7e9a9c9c68dd945ed6890241f39e7d7b652b49c6c2c1902',
    'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
    1, 'schedule-policy:6666666666666666666666666666666666666666666666666666666666666666',
    'capture-scope:6666666666666666666666666666666666666666666666666666666666666666',
    '9622c0d877c821782d2a412b7ceaa6c8c465dc18ff2bd647d8b42010af25fa1f'
);
insert into raw.capture_campaign_list_versions (campaign_id, list_version_id) values (
    'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
    'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef'
);
insert into raw.capture_obligations (
    obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
    capture_requirement_id, partition_key, content_sha256
) values (
    'capture-list-obligation:261a5d6ccd4e326894c240a932c9fdb0f892bdbebfd991012c4243b0210294e6',
    'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
    'capture-run:97af27541be16406f7e9a9c9c68dd945ed6890241f39e7d7b652b49c6c2c1902',
    'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef',
    'listing', 'listing:xnas:goog', 'market-price:v1', '2026-03-31',
    '261a5d6ccd4e326894c240a932c9fdb0f892bdbebfd991012c4243b0210294e6'
);

do $$ begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, predicate, selected_obligation_ids,
            planner_version, content_sha256
        ) values (
            'capture-list-recapture-plan:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            '2026-04-01T00:00:00Z', repeat('b', 64), jsonb_build_object(
                'predicate_id', 'recapture-predicate:' || repeat('b', 64),
                'content_sha256', repeat('b', 64),
                'universe_refs', '[]'::jsonb,
                'subject_ids', '[]'::jsonb,
                'source_policy_ids', '[]'::jsonb,
                'semantic_types', '[]'::jsonb,
                'partitions', '[]'::jsonb,
                'terminal_states', '[]'::jsonb,
                'freshness_states', '[]'::jsonb,
                'parser_versions', '[]'::jsonb,
                'mapping_versions', '[]'::jsonb,
                'assessment_policy_ids', '[]'::jsonb
            ), array['capture-list-obligation:261a5d6ccd4e326894c240a932c9fdb0f892bdbebfd991012c4243b0210294e6'],
            'capture-planner:v1', repeat('b', 64)
        );
        raise exception 'unbounded typed predicate unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$
declare
    candidate record;
    universe_payload jsonb;
    obligation_identity jsonb;
    obligation_payload jsonb;
    payload jsonb;
begin
    universe_payload := jsonb_build_object(
        'universe_id', 'universe:topt-us-2026-03-31',
        'universe_version', 'topt-sql-contract-v1',
        'content_sha256', repeat('8', 64)
    );
    for candidate in
        select * from (values
            ('market-price:latest', '2026-03-31', 'mutable capture requirement'),
            ('market-price:v1', 'bad partition', 'malformed partition')
        ) as candidates(requirement_id, partition_key, description)
    loop
        obligation_identity := jsonb_build_object(
            'run_id', 'capture-run:97af27541be16406f7e9a9c9c68dd945ed6890241f39e7d7b652b49c6c2c1902',
            'universe_ref', universe_payload,
            'subject', jsonb_build_object('kind', 'listing', 'id', 'listing:xnas:goog'),
            'capture_requirement_id', candidate.requirement_id,
            'partition', candidate.partition_key
        );
        obligation_payload := obligation_identity || jsonb_build_object(
            'obligation_id', 'list-obligation:' || raw.canonical_sha256(
                jsonb_build_object('kind', 'list-obligation', 'identity', obligation_identity)
            ),
            'content_sha256', raw.canonical_sha256(obligation_identity)
        );
        payload := jsonb_build_object(
            'list_version_id', 'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef',
            'obligation', obligation_payload
        );
        begin
            insert into raw.capture_obligations (
                obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
                capture_requirement_id, partition_key, content_sha256
            ) values (
                'capture-list-obligation:' || raw.canonical_sha256(payload),
                'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
                'capture-run:97af27541be16406f7e9a9c9c68dd945ed6890241f39e7d7b652b49c6c2c1902',
                'list-version:503cdf7ca54bc8f7873993cc1dd1ad6ce9105ade759640c041eb145130bff3ef',
                'listing', 'listing:xnas:goog', candidate.requirement_id, candidate.partition_key,
                raw.canonical_sha256(payload)
            );
            raise exception '% unexpectedly succeeded', candidate.description;
        exception when check_violation then null;
        end;
    end loop;
end $$;
insert into raw.capture_source_requests (
    source_request_id, content_sha256, source_registry_entry_id, source_policy_id,
    request_fingerprint_version, canonical_request_sha256, subject_refs,
    capture_requirement_ids, partition_key, payload
) values (
    'source-request:6666666666666666666666666666666666666666666666666666666666666666',
    repeat('6', 64),
    'source-registry-entry:6666666666666666666666666666666666666666666666666666666666666666',
    'source-policy:sql-contract-v1', 'sql-contract-request:v1', repeat('6', 64),
    '[{"kind":"listing","id":"listing:xnas:goog"}]'::jsonb,
    array['market-price:v1'], '2026-03-31', '{}'::jsonb
);
insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
    retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
) values (
    'capture-work-item:bd49b6a7ab06f9db999456b28a01a7121526a3b90de2c72fc2899ea64decadef',
    'capture-campaign:9480d69e5ddf795495b5995cfe1afb2b4b4a40899bc5122c30064e1680d471cb',
    'source-request:6666666666666666666666666666666666666666666666666666666666666666',
    'schedule-policy:6666666666666666666666666666666666666666666666666666666666666666',
    1, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
    array['failed', 'success', 'unavailable', 'unchanged'],
    '35975634c3501a457f2f513d275fb6898663d5538c57834f38c919db9a78b941',
    '6e3cbe0a761b6ef3fde767e7b64eff604bbe18017d1a7a8c4cca193fe84bd32d'
);

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, started_at_canonical, content_sha256
) values (
    'fetch-attempt:c93004d370723b15c8cdd222f2c7e27d59d5e66cbc69bc4fe7d929133fd5b3fd',
    'capture-work-item:bd49b6a7ab06f9db999456b28a01a7121526a3b90de2c72fc2899ea64decadef',
    1, '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00',
    '8eed8c9ab19bc80ff2934c724759ea0de51d108a3f34865b1060d97a7077d646'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, completed_at_canonical,
    outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:7a2e540af6cc61373d736bd337803bfe6e475e4a4031eed1e081bc79be575550',
    'fetch-attempt:c93004d370723b15c8cdd222f2c7e27d59d5e66cbc69bc4fe7d929133fd5b3fd',
    '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00',
    'failed', array['offset_fixture'],
    '5c3abf9f5bd78d64cc4ce0d63793b2800f1b956c1865e4de43009540269b9450'
);
insert into raw.capture_checkpoints (
    checkpoint_id, run_id, sequence, phase, completed_obligation_ids,
    recorded_at, recorded_at_canonical, content_sha256
) values (
    'capture-checkpoint:55ee9d997a9bd12b4a2f0aac93046cb1968b7f8d6ea95880db244178e4512742',
    'capture-run:97af27541be16406f7e9a9c9c68dd945ed6890241f39e7d7b652b49c6c2c1902',
    1, 'planned', array[]::text[],
    '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00',
    'fdf99ef5cb21045283335f14f0913ee4784941c20516de1cf0521ad28a4222e4'
);
insert into raw.recapture_plans (
    plan_id, selection_cutoff, selection_cutoff_canonical, predicate_sha256, predicate,
    selected_obligation_ids, planner_version, content_sha256
) values (
    'capture-list-recapture-plan:8ed9769976a46f9ed782c34372ce36977db207c0d343381390d39faabfb3eb33',
    '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+08:00',
    'd2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1',
    '{"assessment_policy_ids":[],"content_sha256":"d2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1","freshness_states":[],"mapping_versions":[],"parser_versions":[],"partitions":[],"predicate_id":"recapture-predicate:9d70275ce843f58202347c2d9d2649da1487756888b3392667fca49078e9ab6e","semantic_types":[],"source_policy_ids":[],"subject_ids":["listing:xnas:goog"],"terminal_states":[],"universe_refs":[]}',
    array['capture-list-obligation:261a5d6ccd4e326894c240a932c9fdb0f892bdbebfd991012c4243b0210294e6'],
    'capture-planner:v1', '2f25f2071754118842a1a37505ce1b6d0a0177f7968d1e5c0fa982d395c7f812'
);

do $$
declare
    candidate record;
begin
    for candidate in
        select * from (values
            ('{predicate_id}'::text[], 'null'::jsonb, 'null predicate ID'),
            ('{semantic_types,0}'::text[], '42'::jsonb, 'non-string predicate coordinate'),
            ('{terminal_states}'::text[], '["not_terminal"]'::jsonb, 'unknown terminal state')
        ) as candidates(path, replacement, description)
    loop
        begin
            insert into raw.recapture_plans (
                plan_id, selection_cutoff, predicate_sha256, predicate,
                selected_obligation_ids, planner_version, content_sha256
            ) select
                'capture-list-recapture-plan:' || repeat('d', 64), selection_cutoff,
                predicate_sha256, jsonb_set(predicate, candidate.path, candidate.replacement),
                selected_obligation_ids, planner_version, repeat('d', 64)
              from raw.recapture_plans
             where plan_id = 'capture-list-recapture-plan:8ed9769976a46f9ed782c34372ce36977db207c0d343381390d39faabfb3eb33';
            raise exception '% unexpectedly succeeded', candidate.description;
        exception when check_violation then null;
        end;
    end loop;
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, predicate,
            selected_obligation_ids, planner_version, content_sha256
        ) select
            'capture-list-recapture-plan:' || repeat('e', 64), selection_cutoff,
            predicate_sha256, predicate, selected_obligation_ids, 'bad planner', repeat('e', 64)
          from raw.recapture_plans
         where plan_id = 'capture-list-recapture-plan:8ed9769976a46f9ed782c34372ce36977db207c0d343381390d39faabfb3eb33';
        raise exception 'malformed planner version unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, predicate, selected_obligation_ids,
            planner_version, content_sha256
        ) select
            'capture-list-recapture-plan:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            selection_cutoff, predicate_sha256, predicate, selected_obligation_ids,
            'capture-planner:main', repeat('c', 64)
          from raw.recapture_plans
         where plan_id = 'capture-list-recapture-plan:8ed9769976a46f9ed782c34372ce36977db207c0d343381390d39faabfb3eb33';
        raise exception 'mutable planner version unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

-- These constants are generated by the Python immutable contracts. Successful
-- inserts therefore prove Python/Postgres canonical JSON and SHA-256 parity.
insert into raw.capture_campaigns (
    campaign_id, content_sha256, policy_id, environment, cutoff, universe_refs
) values (
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    '1ab31dfddd59872f0bc9af7d4dda064ed3b16c0883b7935c4cdc75e13b0b0588',
    'capture-policy:d5-tiny:v1', 'github_ci', '2026-04-01T00:00:00Z',
    '[{"universe_id":"universe:topt-us-2026-03-31","universe_version":"topt-sql-contract-v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'
);

insert into raw.capture_schedule_policies (
    schedule_policy_id, content_sha256, policy_version, demanded_cadence,
    provider_availability_cadence, freshness_max_age, retry_policy, payload
) values
    ('schedule-policy:' || repeat('a', 64), repeat('a', 64), 'sql-contract-a:v1', interval '1 day', 'fixture-daily:v1', interval '2 days', '{}'::jsonb, '{}'::jsonb),
    ('schedule-policy:' || repeat('d', 64), repeat('d', 64), 'sql-contract-d:v1', interval '1 day', 'fixture-daily:v1', interval '2 days', '{}'::jsonb, '{}'::jsonb),
    ('schedule-policy:' || repeat('e', 64), repeat('e', 64), 'sql-contract-e:v1', interval '1 day', 'fixture-daily:v1', interval '2 days', '{}'::jsonb, '{}'::jsonb);

insert into raw.capture_runs (
    run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256
) values (
    'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    1, 'schedule-policy:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'capture-scope:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'df08d60d4d02d00dd264ecaa97a1c1881ab0b0e5a27c9a0f7278d986ee3f8557'
);

insert into raw.capture_list_versions (
    list_version_id, universe_id, universe_version, universe_sha256,
    effective_at, member_count, members, content_sha256
) values (
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
    '2026-04-01T00:00:00Z', 2,
    '[{"kind":"listing","id":"listing:xnas:goog"},{"kind":"listing","id":"listing:xnas:googl"}]',
    '07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253'
);

insert into raw.capture_list_version_members (
    list_version_id, member_ordinal, subject_kind, subject_id
) values
('list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253', 1, 'listing', 'listing:xnas:goog'),
('list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253', 2, 'listing', 'listing:xnas:googl');

insert into raw.capture_list_versions (
    list_version_id, universe_id, universe_version, universe_sha256,
    effective_at, member_count, members, content_sha256
) values (
    'list-version:0d312e5b25aa8a450ffefa2c039a1286a035093c3f9a7ef5dc4f47f31cd971a8',
    'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
    '2026-04-01T00:00:00.123000Z', 1,
    '[{"kind":"listing","id":"listing:xnas:goog"}]',
    '0d312e5b25aa8a450ffefa2c039a1286a035093c3f9a7ef5dc4f47f31cd971a8'
);
insert into raw.capture_list_version_members (
    list_version_id, member_ordinal, subject_kind, subject_id
) values (
    'list-version:0d312e5b25aa8a450ffefa2c039a1286a035093c3f9a7ef5dc4f47f31cd971a8',
    1, 'listing', 'listing:xnas:goog'
);

do $$ begin
    begin
        insert into raw.capture_list_versions (
            list_version_id, universe_id, universe_version, universe_sha256,
            effective_at, effective_at_canonical, member_count, members, content_sha256
        ) values (
            'list-version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
            '2026-04-01T08:00:00+08:00', '2026-04-01T08:00:00+07:00', 1,
            '[{"kind":"listing","id":"listing:xnas:goog"}]', repeat('a', 64)
        );
        raise exception 'tampered canonical timestamp unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_campaign_list_versions (campaign_id, list_version_id) values (
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253'
);

insert into raw.capture_obligations (
    obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
    capture_requirement_id, partition_key, content_sha256
) values
(
    'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    'listing', 'listing:xnas:goog', 'market-price:v1', '2026-03-31',
    '3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'
),
(
    'capture-list-obligation:eea906db6112e2937848249cf0cf4c31aa9af7260d2046ee08a43dcd298b9d41',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    'listing', 'listing:xnas:googl', 'market-price:v1', '2026-03-31',
    'eea906db6112e2937848249cf0cf4c31aa9af7260d2046ee08a43dcd298b9d41'
);

insert into raw.capture_source_requests (
    source_request_id, content_sha256, source_registry_entry_id, source_policy_id,
    request_fingerprint_version, canonical_request_sha256, subject_refs,
    capture_requirement_ids, partition_key, payload
)
select
    'source-request:' || repeat(seed, 64), repeat(seed, 64),
    'source-registry-entry:' || repeat(seed, 64), 'source-policy:sql-contract-v1',
    'sql-contract-request:v1', repeat(seed, 64),
    '[{"kind":"listing","id":"listing:xnas:goog"}]'::jsonb,
    array['market-price:v1'], '2026-03-31', '{}'::jsonb
from unnest(array['9', 'd', 'e', 'f']) as seed;

insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
    retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
) values (
    'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'source-request:9999999999999999999999999999999999999999999999999999999999999999',
    'schedule-policy:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    3, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
    array['failed', 'success', 'unavailable', 'unchanged'],
    '50c4053abc97596f04ecc5857d284337dd4b81af14a62f66320c49d9aef19f17',
    '6a300a34b166f67d752d1ba9395151ee758c0864118ad1c1041251881cce9f61'
);

do $$ begin
    begin
        insert into raw.capture_work_items (
            work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
            retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
        ) values (
            'capture-work-item:4c679d3c16ced6641d88e1195a808412ba4af409cf1fc78ff300717291b4abfe',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'source-request:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            'schedule-policy:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            1, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
            array['failed', 'success', 'unavailable', 'unchanged'],
            '5e5f1204dfcb90cc2927d07378dcbb2ec5a5fb9cf805ce24fb8701d73224b8d3', repeat('b', 64)
        );
        raise exception 'mutated storage envelope unexpectedly succeeded';
    exception when check_violation then null;
    end;
    begin
        insert into raw.capture_work_items (
            work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
            retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
        ) values (
            'capture-work-item:78d39274f57a83b752718f3ad26f05059ef36c6e05355c60b98a4edf3d0558ab',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'source-request:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            'schedule-policy:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            2, array['interrupted', 'rate_limited'],
            array['failed', 'interrupted', 'success', 'unavailable', 'unchanged'],
            '64a394a2ef1b0f3c85fde4078d8bd45a96205e794b6939f6a8246c493b76b496',
            '123d393cd464c39d3f1db4823c5d6f2d6f5c23956b13ddc0e15774b6f84fcab4'
        );
        raise exception 'overlapping retry policy unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_work_items (
            work_item_id, campaign_id, source_request_id, schedule_policy_id,
            maximum_attempts, retryable_outcomes, terminal_outcomes,
            content_sha256, storage_envelope_sha256
        ) values (
            'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'source-request:9999999999999999999999999999999999999999999999999999999999999999',
            'schedule-policy:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            4, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
            array['failed', 'success', 'unavailable', 'unchanged'],
            '50c4053abc97596f04ecc5857d284337dd4b81af14a62f66320c49d9aef19f17',
            '42ae75218b75d54b76f5f944a0b0239fa1d472c053378a59536705e92f6feead'
        );
        raise exception 'same work identity with changed retry envelope unexpectedly succeeded';
    exception when unique_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_work_items (
            work_item_id, campaign_id, source_request_id, schedule_policy_id,
            maximum_attempts, retryable_outcomes, terminal_outcomes,
            content_sha256, storage_envelope_sha256
        ) values (
            'capture-work-item:ddc047ae9337de06520b889a05a3b44113d6dfdf72dde9d1809df5e9689bfedb',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'source-request:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'schedule-policy:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            21, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
            array['failed', 'success', 'unavailable', 'unchanged'],
            '221d96c5f497fd7bb9b748bcbd535b32475ab8ccdf3e5fe9c70876f7c5a48ce2',
            '6cdcf29944d6c03fcc4af6e7de56415871f7572a15bc12d266d3452fbc387202'
        );
        raise exception 'retry maximum above shared policy bound unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_obligation_work_bindings (
    binding_id, obligation_id, work_item_id, content_sha256
) values (
    'capture-obligation-work-binding:a6689f8ffcbec151dee7b3cc67a435e66ae23f88563cc125eed3c364246b4918',
    'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
    'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
    '64d38081f1a72ebcde402232a679266db70d388a1688c6bc4a058057c03d7467'
);

insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
    retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
) values
(
    'capture-work-item:2b502d40794610e7662b1cc42233979f84856808e6638bc2fa519e2f24ee6d5e',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'source-request:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    'schedule-policy:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    1, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
    array['failed', 'success', 'unavailable', 'unchanged'],
    '2a2b98a300feaef77d5cdffe0ddfa31227f2d73d3903a0abc117a920bda1bb17',
    '83059192ec66e90c725864c22e142ced24baf6fd3ab519544b25d136b2595516'
),
(
    'capture-work-item:e77f6d81853d8f07cd5aa3462ef9929176cf0395109c412f9c270a3419f73cbb',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'source-request:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    'schedule-policy:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    1, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
    array['failed', 'success', 'unavailable', 'unchanged'],
    '412d85882dd2b70b7c2859382be5029fa81b9f0c77c7402a644dea904e805123',
    'f959baa8a1c58f499cdd20f7e65676a609c50ef6cac8ca65c0f2e9be12607efe'
);

do $$ begin
    begin
        insert into raw.capture_obligation_work_bindings (
            binding_id, obligation_id, work_item_id, content_sha256
        ) values (
            'capture-obligation-work-binding:070bafddaea8433ff9ca82e6e7297e2917a33bdff28b2fce3d872a6de75f3e4e',
            'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
            'capture-work-item:2b502d40794610e7662b1cc42233979f84856808e6638bc2fa519e2f24ee6d5e',
            'fd24f50efe4b51f6933bf80b4e40fc5517cf87c0e7c91268b8e1c10e8e4fcb6d'
        );
        raise exception 'second work binding for one obligation unexpectedly succeeded';
    exception when unique_violation then null;
    end;
    begin
        insert into raw.capture_obligation_work_bindings (
            binding_id, obligation_id, work_item_id, content_sha256
        ) values (
            'capture-obligation-work-binding:c2c4657a3ca7a1a90d7b5974f3f35e2606a3bcb59a9c939c5c8c62068a723f3a',
            'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
            'capture-work-item:e77f6d81853d8f07cd5aa3462ef9929176cf0395109c412f9c270a3419f73cbb',
            '1de41df277f1eeb788eed2586d85e4b7d93241c34a1df18873c88a10f926e208'
        );
        raise exception 'cross-schedule binding unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'cross-schedule binding unexpectedly succeeded' then raise; end if;
    end;
end $$;

insert into raw.capture_campaigns (
    campaign_id, content_sha256, policy_id, environment, cutoff, universe_refs
) values (
    'capture-campaign:1e46ac6e844e76cfa49b0da1e55121885b90149cce3967d0aaa01d7576e242f0',
    'caccb5f9c2acd79b675fc6f6ee9b13ce9f6063830cc2ddc92985f5e0e1a2e300',
    'capture-policy:d5-cross-campaign-negative:v1', 'github_ci', '2026-04-01T00:00:00Z',
    '[{"universe_id":"universe:topt-us-2026-03-31","universe_version":"topt-sql-contract-v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'
);
insert into raw.capture_runs (
    run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256
) values (
    'capture-run:0a53a996e6824ca61739ee690adebd6cdbcbb074e1fbe0f65667e9fb1c3b9274',
    'capture-campaign:1e46ac6e844e76cfa49b0da1e55121885b90149cce3967d0aaa01d7576e242f0',
    1, 'schedule-policy:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    'capture-scope:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    '40bef2acd9fdf0a955f4c90a619afd9a7bc2601434cf0ed99626b371c74f3f3c'
);
insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
    retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
) values (
    'capture-work-item:c9941e95ea943d8e0b3d8388552553fce0e09f2213b3af6a9fa8d9d3dc487cf3',
    'capture-campaign:1e46ac6e844e76cfa49b0da1e55121885b90149cce3967d0aaa01d7576e242f0',
    'source-request:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    'schedule-policy:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    2, array['interrupted', 'server_error', 'transport_error'],
    array['failed', 'rate_limited', 'success', 'unavailable', 'unchanged'],
    '4f6d691e4a242027a1e884ed8f83061ad414c8f9cdb00d191329f3d62be191ea',
    '63690493457c6ed88214a70091080d03d432a5381a71f69b7aa4312f250a4eff'
);

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:f2e14226d3ee4304a090048f368355f61c1e25c27a1ad47dec17575c9e6984e5',
    'capture-work-item:c9941e95ea943d8e0b3d8388552553fce0e09f2213b3af6a9fa8d9d3dc487cf3',
    1, '2026-04-01T00:00:00Z', '59be21a64eaac1cc93f61de45e27c983c94c862472b8ab45560d429bcdd7ccb9'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, status_code, reason_codes, content_sha256
) values (
    'fetch-attempt-result:4a39294c1bef30284466fe32a202a4672632fd707b8b297352d0a22fce75a182',
    'fetch-attempt:f2e14226d3ee4304a090048f368355f61c1e25c27a1ad47dec17575c9e6984e5',
    '2026-04-01T00:00:00Z', 'rate_limited', 429, array['policy_terminal'],
    '09f78ab58647e42b0de730ce2d99c3c3fac6548ac051cca2d24929e50ac6b63f'
);
do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:d8afe6424e2ca263757ee738deaf5e0bc10603c9b3fa7ea6c1a1a1e539f9f8db',
            'capture-work-item:c9941e95ea943d8e0b3d8388552553fce0e09f2213b3af6a9fa8d9d3dc487cf3',
            2, '2026-04-01T00:00:00Z',
            '08505b7cef668ad37723b6d612fc540600c45a5c645e3cf48fd68af27515074d'
        );
        raise exception 'attempt after policy-terminal rate limit unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'attempt after policy-terminal rate limit unexpectedly succeeded' then raise; end if;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'capture-list-obligation:e37ca70c93551415a597c958d86000387ff888602be4b951377d7b3635120eaf',
            'capture-campaign:1e46ac6e844e76cfa49b0da1e55121885b90149cce3967d0aaa01d7576e242f0',
            'capture-run:0a53a996e6824ca61739ee690adebd6cdbcbb074e1fbe0f65667e9fb1c3b9274',
            'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
            'listing', 'listing:xnas:goog', 'market-price:v1', '2026-03-31',
            'e37ca70c93551415a597c958d86000387ff888602be4b951377d7b3635120eaf'
        );
        raise exception 'undeclared campaign list version unexpectedly succeeded';
    exception when foreign_key_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_obligation_work_bindings (
            binding_id, obligation_id, work_item_id, content_sha256
        ) values (
            'capture-obligation-work-binding:beb5fb3494d8c7abe243f3fa6b41973269c899ab0248f825b04ecbc01fa835e6',
            'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
            'capture-work-item:c9941e95ea943d8e0b3d8388552553fce0e09f2213b3af6a9fa8d9d3dc487cf3',
            'f388d58c17ddaf6ccc6b7c5fdee067d699edbbe9e5729dd593b42c7c19795f13'
        );
        raise exception 'cross-campaign binding unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'cross-campaign binding unexpectedly succeeded' then raise; end if;
    end;
end $$;

-- Shape-correct but content-incorrect addresses must fail at the database boundary.
do $$ begin
    begin
        insert into raw.capture_campaigns (
            campaign_id, content_sha256, policy_id, environment, cutoff, universe_refs
        ) values (
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            repeat('a', 64), 'capture-policy:wrong:v1', 'github_ci', '2026-04-01T00:00:00Z',
            '[{"universe_id":"universe:topt-us-2026-03-31","universe_version":"topt-sql-contract-v1","content_sha256":"8888888888888888888888888888888888888888888888888888888888888888"}]'
        );
        raise exception 'mismatched campaign address unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_list_versions (
            list_version_id, universe_id, universe_version, universe_sha256,
            effective_at, member_count, members, content_sha256
        ) values (
            'list-version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
            '2026-04-01T00:00:00Z', 2,
            '[{"kind":"listing","id":"listing:xnas:googl"},{"kind":"listing","id":"listing:xnas:goog"}]',
            repeat('a', 64)
        );
        raise exception 'noncanonical member order unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'capture-list-obligation:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
            'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
            'listing', 'listing:xnas:goog', 'market-price:v1', '2026-03-30', repeat('a', 64)
        );
        raise exception 'mismatched obligation address unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

-- Attempt identity excludes dispatch time while content includes it, matching Python.
insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:4703dd01bf0c3abd4849a04d9997a1ce45ceb0793bcd97820dbde2fe5765d3c4',
    'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
    1, '2026-04-01T00:01:00Z', 'f4a2fdf03b4df968c03e9c6284c0d6b94ae3bb12342ff0278e0e94a73302b8b4'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:ec5faa9d229ff502efbe76f2e80c30601c93ecf4224787e1bd3c8bf048f68962',
    'fetch-attempt:4703dd01bf0c3abd4849a04d9997a1ce45ceb0793bcd97820dbde2fe5765d3c4',
    '2026-04-01T00:01:01Z', 'interrupted', array['retry'],
    'acea7ae2639221865c7354867d57da04309a4938a83769012e1fe3db046a0d38'
);

do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:041e13b06db57a3f5ebef81b6c3a155a18c92b2e1457cff811fcb15566d05adb',
            'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
            2, '2026-04-01T00:01:00Z', repeat('a', 64)
        );
        raise exception 'backdated retry unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'backdated retry unexpectedly succeeded' then raise; end if;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:041e13b06db57a3f5ebef81b6c3a155a18c92b2e1457cff811fcb15566d05adb',
            'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
            2, '2026-04-01T00:01:02Z', repeat('a', 64)
        );
        raise exception 'mismatched attempt content unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:041e13b06db57a3f5ebef81b6c3a155a18c92b2e1457cff811fcb15566d05adb',
    'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
    2, '2026-04-01T00:01:02Z', 'cdafb3803ea23384eee4a2d0ce23764c5f5dfac454f3a27429422a58602c0782'
);

do $$ begin
    begin
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'fetch-attempt:041e13b06db57a3f5ebef81b6c3a155a18c92b2e1457cff811fcb15566d05adb',
            '2026-04-01T00:01:03Z', 'rate_limited', array['retry'], repeat('a', 64)
        );
        raise exception 'mismatched attempt result unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:ddbb457010b0d14cc1cad83dff1ea88323f5c1828280332e0d70e898d5cffd15',
    'fetch-attempt:041e13b06db57a3f5ebef81b6c3a155a18c92b2e1457cff811fcb15566d05adb',
    '2026-04-01T00:01:03Z', 'rate_limited', array['retry'],
    '6df17944e9cdb0a40bf34e91f7826037fca4a6fe2134bbf9a7f6c2aad2f3e86b'
);

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:0d853af574ad2cac226735de07534d4cb1f8043f5b130b151058debec3bf35c2',
    'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
    3, '2026-04-01T00:01:04Z', 'd1c2351cfc549b3dd862354f09e929c581be2afd4e9ddb06268634b0386026c4'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, status_code, reason_codes, content_sha256
) values (
    'fetch-attempt-result:2a2c52b24cdfef1815e469ee3c2fcabb4245a7fcd11787fdfd45b40706ec3fd6',
    'fetch-attempt:0d853af574ad2cac226735de07534d4cb1f8043f5b130b151058debec3bf35c2',
    '2026-04-01T00:01:05Z', 'failed', 503, array['fixture_failure'],
    '8c90fc336e7d27bf956830edaa236eb3999abc8775d9cd82c20cbf4014ec109a'
);

do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-work-item:25e1905020c88a8414c9be010be32b62d3aed62e77e09b3e85e90a70a60bfe81',
            4, '2026-04-01T00:01:06Z', repeat('a', 64)
        );
        raise exception 'attempt above maximum unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'attempt above maximum unexpectedly succeeded' then raise; end if;
    end;
end $$;

insert into raw.capture_checkpoints (
    checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
) values (
    'capture-checkpoint:e97c50e3bf069ba0f124e3c20acdf55a527067c3683e96a7c1d90d0c86c94ced',
    'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
    1, 'raw_landed', array['capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'],
    '2026-04-01T00:02:00Z', '08e2b19b84e71bc34e040a43f966cbb583526b8289d12d388f5eb2fc8c8304e8'
);

do $$ begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:556a3b383524b700828df90edf16ef60c686f6f106ee4019d1a5600ecbbacc20',
            'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
            2, 'normalized', array['capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'],
            '2026-04-01T00:01:59Z', '8c98833d9739c49e2b139c15586a2aabab6750a9a4564b145ac150f98cac1c4d'
        );
        raise exception 'backdated checkpoint unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'backdated checkpoint unexpectedly succeeded' then raise; end if;
    end;
end $$;

do $$ begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:556a3b383524b700828df90edf16ef60c686f6f106ee4019d1a5600ecbbacc20',
            'capture-run:2affe53c91561a46fb296c18b4819ad9d2fcec9d40922e40db00bb74f7f483a6',
            2, 'normalized', array['capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'],
            '2026-04-01T00:02:01Z', repeat('a', 64)
        );
        raise exception 'mismatched checkpoint content unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.recapture_plans (
    plan_id, selection_cutoff, predicate_sha256, predicate, selected_obligation_ids,
    planner_version, content_sha256
) values (
    'capture-list-recapture-plan:61ae610f9f6ad21f0fbdf51dd4550cc86b58575f363d33ba967868911020ce46',
    '2026-04-01T00:00:00Z', 'd2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1',
    '{"assessment_policy_ids":[],"content_sha256":"d2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1","freshness_states":[],"mapping_versions":[],"parser_versions":[],"partitions":[],"predicate_id":"recapture-predicate:9d70275ce843f58202347c2d9d2649da1487756888b3392667fca49078e9ab6e","semantic_types":[],"source_policy_ids":[],"subject_ids":["listing:xnas:goog"],"terminal_states":[],"universe_refs":[]}',
    array['capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'],
    'capture-planner:v1', '1b20a95545bb0915fb932af45defd50c5dffa848af607c14abf62d61d564b65b'
);

do $$ begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, predicate, selected_obligation_ids,
            planner_version, content_sha256
        ) values (
            'capture-list-recapture-plan:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-04-01T00:00:00Z', repeat('a', 64), '{"assessment_policy_ids":[],"content_sha256":"d2bfbc83c9f70d19249adfadbe4df9b3ddd8e3dd5eb536fca31fc22af492d0f1","freshness_states":[],"mapping_versions":[],"parser_versions":[],"partitions":[],"predicate_id":"recapture-predicate:9d70275ce843f58202347c2d9d2649da1487756888b3392667fca49078e9ab6e","semantic_types":[],"source_policy_ids":[],"subject_ids":["listing:xnas:goog"],"terminal_states":[],"universe_refs":[]}',
            array['capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac'],
            'capture-planner:v1', repeat('a', 64)
        );
        raise exception 'mismatched recapture predicate unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

do $$ begin
    begin
        update raw.capture_obligations set subject_id = 'listing:xnas:collapsed';
        raise exception 'append-only update unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'append-only update unexpectedly succeeded' then raise; end if;
    end;
end $$;

do $$ declare obligation_count integer; begin
    select count(*) into obligation_count from raw.capture_obligations;
    if obligation_count <> 3 then
        raise exception 'GOOG and GOOGL obligation identities collapsed';
    end if;
end $$;

rollback;
