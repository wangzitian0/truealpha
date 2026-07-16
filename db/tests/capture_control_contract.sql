\set ON_ERROR_STOP on

begin;

insert into raw.capture_campaigns (
    campaign_id, content_sha256, policy_id, environment, cutoff
) values (
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    repeat('a', 64), 'capture-policy:d5-tiny:v1', 'github_ci', '2026-04-01T00:00:00Z'
);

insert into raw.capture_list_versions (
    list_version_id, universe_id, universe_version, universe_sha256,
    effective_at, member_count, content_sha256
) values (
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    'universe:topt-us-2026-03-31', 'topt-sql-contract-v1', repeat('8', 64),
    '2026-04-01T00:00:00Z', 2, '07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253'
);

insert into raw.capture_list_version_members (
    list_version_id, member_ordinal, subject_kind, subject_id
) values
(
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    1, 'listing', 'listing:xnas:goog'
),
(
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253',
    2, 'listing', 'listing:xnas:googl'
);

insert into raw.capture_obligations (
    obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
    capture_requirement_id, partition_key, content_sha256
) values
(
    'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253', 'listing', 'listing:xnas:goog',
    'market-price:v1', '2026-03-31', repeat('b', 64)
),
(
    'capture-list-obligation:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253', 'listing', 'listing:xnas:googl',
    'market-price:v1', '2026-03-31', repeat('c', 64)
);

do $$
begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'capture-list-obligation:7777777777777777777777777777777777777777777777777777777777777777',
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'list-version:not-canonical', 'listing', 'listing:xnas:invalid',
            'market-price:v1', '2026-03-31', repeat('7', 64)
        );
        raise exception 'non-canonical list version unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'capture-list-obligation:6666666666666666666666666666666666666666666666666666666666666666',
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'list-version:6666666666666666666666666666666666666666666666666666666666666666',
            'listing', 'listing:xnas:goog', 'market-price:v1', '2026-03-31', repeat('6', 64)
        );
        raise exception 'unpersisted list version unexpectedly succeeded';
    exception when raise_exception or foreign_key_violation then
        if sqlerrm = 'unpersisted list version unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts, content_sha256
) values (
    'capture-work-item:9999999999999999999999999999999999999999999999999999999999999999',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'source-request:9999999999999999999999999999999999999999999999999999999999999999',
    'schedule-policy:9999999999999999999999999999999999999999999999999999999999999999',
    3, repeat('9', 64)
);

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1',
    'capture-work-item:9999999999999999999999999999999999999999999999999999999999999999',
    1, '2026-04-01T00:01:00Z', repeat('1', 64)
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1',
    'fetch-attempt:a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1',
    '2026-04-01T00:01:01Z', 'interrupted', array['retry'], repeat('1', 64)
);
insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2',
    'capture-work-item:9999999999999999999999999999999999999999999999999999999999999999',
    2, '2026-04-01T00:01:02Z', repeat('2', 64)
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2',
    'fetch-attempt:a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2',
    '2026-04-01T00:01:03Z', 'rate_limited', array['retry'], repeat('2', 64)
);
insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3',
    'capture-work-item:9999999999999999999999999999999999999999999999999999999999999999',
    3, '2026-04-01T00:01:04Z', repeat('3', 64)
);
insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3',
    'fetch-attempt:a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3',
    '2026-04-01T00:01:05Z', 'server_error', array['retry'], repeat('3', 64)
);

do $$
begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4',
            'capture-work-item:9999999999999999999999999999999999999999999999999999999999999999',
            4, '2026-04-01T00:01:06Z', repeat('4', 64)
        );
        raise exception 'attempt beyond frozen maximum unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'attempt beyond frozen maximum unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:7777777777777777777777777777777777777777777777777777777777777777',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            1, 'raw_landed', array['capture-list-obligation:not-canonical'],
            '2026-04-01T00:02:00Z', repeat('7', 64)
        );
        raise exception 'malformed checkpoint obligation unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:8888888888888888888888888888888888888888888888888888888888888888',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            1, 'raw_landed', array[
                'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            ], '2026-04-01T00:02:00Z', repeat('8', 64)
        );
        raise exception 'duplicate checkpoint obligations unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, selected_obligation_ids,
            planner_version, content_sha256
        ) values (
            'recapture-plan:8888888888888888888888888888888888888888888888888888888888888888',
            '2026-04-01T00:00:00Z', repeat('8', 64), array[
                'capture-list-obligation:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            ], 'capture-planner:v1', repeat('8', 64)
        );
        raise exception 'unsorted recapture obligations unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, selected_obligation_ids,
            planner_version, content_sha256
        ) values (
            'recapture-plan:7777777777777777777777777777777777777777777777777777777777777777',
            '2026-04-01T00:00:00Z', repeat('7', 64), array[null]::text[],
            'capture-planner:v1', repeat('7', 64)
        );
        raise exception 'null recapture obligation unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.recapture_plans (
            plan_id, selection_cutoff, predicate_sha256, selected_obligation_ids,
            planner_version, content_sha256
        ) values (
            'recapture-plan:6666666666666666666666666666666666666666666666666666666666666666',
            '2026-04-01T00:00:00Z', repeat('6', 64), array[
                'capture-list-obligation:6666666666666666666666666666666666666666666666666666666666666666'
            ], 'capture-planner:v1', repeat('6', 64)
        );
        raise exception 'unknown recapture obligation unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'unknown recapture obligation unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:6666666666666666666666666666666666666666666666666666666666666666',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            1, 'raw_landed', array[
                'capture-list-obligation:6666666666666666666666666666666666666666666666666666666666666666'
            ], '2026-04-01T00:02:00Z', repeat('6', 64)
        );
        raise exception 'unknown checkpoint obligation unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'unknown checkpoint obligation unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

insert into raw.capture_checkpoints (
    checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
) values (
    'capture-checkpoint:1111111111111111111111111111111111111111111111111111111111111111',
    'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    1, 'raw_landed', array[
        'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    ], '2026-04-01T00:02:00Z', repeat('1', 64)
);

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:2222222222222222222222222222222222222222222222222222222222222222',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            3, 'normalized', array[
                'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            ], '2026-04-01T00:02:01Z', repeat('2', 64)
        );
        raise exception 'checkpoint sequence gap unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'checkpoint sequence gap unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:3333333333333333333333333333333333333333333333333333333333333333',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            2, 'planned', array[
                'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            ], '2026-04-01T00:02:01Z', repeat('3', 64)
        );
        raise exception 'checkpoint phase regression unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'checkpoint phase regression unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_checkpoints (
            checkpoint_id, run_id, sequence, phase, completed_obligation_ids, recorded_at, content_sha256
        ) values (
            'capture-checkpoint:4444444444444444444444444444444444444444444444444444444444444444',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            2, 'normalized', array[]::text[], '2026-04-01T00:02:01Z', repeat('4', 64)
        );
        raise exception 'checkpoint obligation regression unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'checkpoint obligation regression unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, run_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'capture-list-obligation:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'capture-run:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'list-version:07c5571460a288c39fb2aa22ec9ec115f44e8f510f7bfae5b76001aadd141253', 'listing', 'listing:xnas:goog',
            'market-price:v1', '2026-03-31', repeat('d', 64)
        );
        raise exception 'duplicate logical obligation unexpectedly succeeded';
    exception when unique_violation then null;
    end;
end;
$$;

insert into raw.capture_work_items (
    work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts, content_sha256
) values (
    'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'source-request:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    'schedule-policy:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    3,
    repeat('e', 64)
);

do $$
begin
    begin
        insert into raw.capture_work_items (
            work_item_id, campaign_id, source_request_id, schedule_policy_id, maximum_attempts, content_sha256
        ) values (
            'capture-work-item:4444444444444444444444444444444444444444444444444444444444444444',
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'source-request:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            'schedule-policy:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            3,
            repeat('4', 64)
        );
        raise exception 'duplicate logical work item unexpectedly succeeded';
    exception when unique_violation then null;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
            'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            2, '2026-04-01T00:00:00Z', repeat('f', 64)
        );
        raise exception 'non-contiguous attempt unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'non-contiguous attempt unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        delete from raw.capture_obligations
         where obligation_id = 'capture-list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
        raise exception 'append-only delete unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'append-only delete unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

insert into raw.capture_attempts (
    attempt_id, work_item_id, attempt_number, started_at, content_sha256
) values (
    'fetch-attempt:1111111111111111111111111111111111111111111111111111111111111111',
    'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    1, '2026-04-01T00:00:00Z', repeat('1', 64)
);

do $$
begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:5555555555555555555555555555555555555555555555555555555555555555',
            'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            2, '2026-04-01T00:00:01Z', repeat('5', 64)
        );
        raise exception 'attempt before prior result unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'attempt before prior result unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:5555555555555555555555555555555555555555555555555555555555555555',
            'fetch-attempt:1111111111111111111111111111111111111111111111111111111111111111',
            '2026-03-31T23:59:59Z', 'interrupted', array['clock_skew'], repeat('5', 64)
        );
        raise exception 'completion before dispatch unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'completion before dispatch unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:2222222222222222222222222222222222222222222222222222222222222222',
            'fetch-attempt:2222222222222222222222222222222222222222222222222222222222222222',
            '2026-04-01T00:00:01Z', 'failed', array['missing'], repeat('2', 64)
        );
        raise exception 'result without dispatch unexpectedly succeeded';
    exception when raise_exception or foreign_key_violation then
        if sqlerrm = 'result without dispatch unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:6666666666666666666666666666666666666666666666666666666666666666',
            'fetch-attempt:1111111111111111111111111111111111111111111111111111111111111111',
            '2026-04-01T00:00:01Z', 'success', array['captured'], repeat('6', 64)
        );
        raise exception 'success without source vintage unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;

insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:3333333333333333333333333333333333333333333333333333333333333333',
    'fetch-attempt:1111111111111111111111111111111111111111111111111111111111111111',
    '2026-04-01T00:00:01Z', 'failed', array['fixture_failure'], repeat('3', 64)
);

do $$
begin
    begin
        insert into raw.capture_attempts (
            attempt_id, work_item_id, attempt_number, started_at, content_sha256
        ) values (
            'fetch-attempt:3333333333333333333333333333333333333333333333333333333333333333',
            'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            2, '2026-04-01T00:00:02Z', repeat('3', 64)
        );
        raise exception 'retry after terminal outcome unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'retry after terminal outcome unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
begin
    begin
        update raw.capture_obligations set subject_id = 'listing:xnas:collapsed';
        raise exception 'append-only update unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'append-only update unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

do $$
declare
    obligation_count integer;
begin
    select count(*) into obligation_count from raw.capture_obligations;
    if obligation_count <> 2 then
        raise exception 'GOOG and GOOGL obligation identities collapsed';
    end if;
end;
$$;

rollback;
