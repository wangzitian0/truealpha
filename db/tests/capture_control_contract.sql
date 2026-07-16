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
