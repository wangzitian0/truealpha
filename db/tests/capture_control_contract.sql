\set ON_ERROR_STOP on

begin;

insert into raw.capture_campaigns (
    campaign_id, content_sha256, policy_id, environment, cutoff
) values (
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    repeat('a', 64), 'capture-policy:d5-tiny:v1', 'github_ci', '2026-04-01T00:00:00Z'
);

insert into raw.capture_obligations (
    obligation_id, campaign_id, list_version_id, subject_kind, subject_id,
    capture_requirement_id, partition_key, content_sha256
) values
(
    'list-obligation:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'list-version:d5-primary-v1', 'listing', 'listing:xnas:goog',
    'market-price:v1', '2026-03-31', repeat('b', 64)
),
(
    'list-obligation:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'list-version:d5-primary-v1', 'listing', 'listing:xnas:googl',
    'market-price:v1', '2026-03-31', repeat('c', 64)
);

do $$
begin
    begin
        insert into raw.capture_obligations (
            obligation_id, campaign_id, list_version_id, subject_kind, subject_id,
            capture_requirement_id, partition_key, content_sha256
        ) values (
            'list-obligation:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'list-version:d5-primary-v1', 'listing', 'listing:xnas:goog',
            'market-price:v1', '2026-03-31', repeat('d', 64)
        );
        raise exception 'duplicate logical obligation unexpectedly succeeded';
    exception when unique_violation then null;
    end;
end;
$$;

insert into raw.capture_work_items (
    work_item_id, campaign_id, request_id, content_sha256
) values (
    'capture-work-item:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    'capture-campaign:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'source-request:fixture', repeat('e', 64)
);

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
        insert into raw.capture_attempt_results (
            attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
        ) values (
            'fetch-attempt-result:missing',
            'fetch-attempt:2222222222222222222222222222222222222222222222222222222222222222',
            '2026-04-01T00:00:01Z', 'failed', array['missing'], repeat('2', 64)
        );
        raise exception 'result without dispatch unexpectedly succeeded';
    exception when raise_exception then
        if sqlerrm = 'result without dispatch unexpectedly succeeded' then raise; end if;
    end;
end;
$$;

insert into raw.capture_attempt_results (
    attempt_result_id, attempt_id, completed_at, outcome, reason_codes, content_sha256
) values (
    'fetch-attempt-result:terminal',
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
