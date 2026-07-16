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

insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts,
    retryable_outcomes, terminal_outcomes, content_sha256, storage_envelope_sha256
) values (
    'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
    'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
    'source-request:9999999999999999999999999999999999999999999999999999999999999999',
    'schedule-policy:9999999999999999999999999999999999999999999999999999999999999999',
    3, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
    array['failed', 'success', 'unavailable', 'unchanged'],
    '5f045ca0f5b4169e0c1dffef3080534f4d9924ad8672220d6e4ed3788e646964',
    '4d04d9077001e2da9ca0794d2b60c80c1d596905408ebc06ceb272d72e5a8fdd'
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
            'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
            'capture-campaign:70ce3ee59a9026d15385946f4b0c798bcd49fd25b056c00dbf44d3d8ebbffee5',
            'source-request:9999999999999999999999999999999999999999999999999999999999999999',
            'schedule-policy:9999999999999999999999999999999999999999999999999999999999999999',
            4, array['interrupted', 'rate_limited', 'server_error', 'transport_error'],
            array['failed', 'success', 'unavailable', 'unchanged'],
            '5f045ca0f5b4169e0c1dffef3080534f4d9924ad8672220d6e4ed3788e646964',
            '76bd8e6c4726c04f398bd52ee539b23b96dee654e5ac12b03c6392e33a95990a'
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
    'capture-obligation-work-binding:850854bbdd96755fb09d96540e5959c89d8dfabcc19010134243e62fd57b8fc6',
    'capture-list-obligation:3970939515b9abea8e87e25bdbe7ea21f1ed3f50a0afd007005f94709fae7eac',
    'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
    'c854e7ff2957e3d8e5778f82bcda3391a9e25eca5e8e7cd7ac6276876489f8f7'
);

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
    'fetch-attempt:df166738a7cb93ff784d9640cda0438aafa61d711eb3a4b7bc790e3bba00ff13',
    'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
    1, '2026-04-01T00:01:00Z', '2ac4f30fb41d2e76608cac846a50dd6447e72d88a55d51e2510e778c23194c7e'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:2dddb7c76a0c4c49af1112cd510ac9f649e4a508c087b000c76d0d5c2ac935ee',
    'fetch-attempt:df166738a7cb93ff784d9640cda0438aafa61d711eb3a4b7bc790e3bba00ff13',
    '2026-04-01T00:01:01Z', 'interrupted', array['retry'],
    'b0a0fb161abd9665487b9ce5fe052e909cfb20514cfdf22a90ef0c5cb24f130d'
);

do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:cdb5883072f178863a87dd6744c52148706ebf2c2d7d14d856d123740937fe23',
            'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
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
            'fetch-attempt:cdb5883072f178863a87dd6744c52148706ebf2c2d7d14d856d123740937fe23',
            'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
            2, '2026-04-01T00:01:02Z', repeat('a', 64)
        );
        raise exception 'mismatched attempt content unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:cdb5883072f178863a87dd6744c52148706ebf2c2d7d14d856d123740937fe23',
    'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
    2, '2026-04-01T00:01:02Z', '7cb8955caad6a865ad4278ce4fea35da84345c5e3daad3fa79abcbd0698c6a4b'
);

do $$ begin
    begin
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'fetch-attempt:cdb5883072f178863a87dd6744c52148706ebf2c2d7d14d856d123740937fe23',
            '2026-04-01T00:01:03Z', 'rate_limited', array['retry'], repeat('a', 64)
        );
        raise exception 'mismatched attempt result unexpectedly succeeded';
    exception when check_violation then null;
    end;
end $$;

insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:adca14280e5bcb9626a3d653fceeb3fd0c9accf5de0aad4d488178a5dc58d380',
    'fetch-attempt:cdb5883072f178863a87dd6744c52148706ebf2c2d7d14d856d123740937fe23',
    '2026-04-01T00:01:03Z', 'rate_limited', array['retry'],
    '856372587fd96f0be6a49c65c78b6999e0a1f3862acfdea623d4470a0489f329'
);

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:d3e59400db48f814647bb3a4c48ab2eb297f73ad0162af091d02c12ad6045d2a',
    'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
    3, '2026-04-01T00:01:04Z', '04f4210b35ccd6c07b3a011a3c8918415ed52d8950bc42fcd62893cc45c67f03'
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, status_code, reason_codes, content_sha256
) values (
    'fetch-attempt-result:b1fa0a61def1bf7436d8d36b2456e9085536ea1b0ebe35123f12485a13387be2',
    'fetch-attempt:d3e59400db48f814647bb3a4c48ab2eb297f73ad0162af091d02c12ad6045d2a',
    '2026-04-01T00:01:05Z', 'failed', 503, array['fixture_failure'],
    '0389bc6b0cff20cd84d9c39471d0b442b1e8aa73819322a7ff66ef5936cc42f4'
);

do $$ begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-work-item:2c6e08d49a213d0570374e51ed84cdaa858fadee33389ecee12761a3da6a4aad',
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
