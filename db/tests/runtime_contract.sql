begin;

do $$
declare
    default_expression text;
begin
    if to_regclass('raw.fetches') is null then
        raise exception 'raw.fetches is missing';
    end if;
    if to_regclass('staging.kg_edges') is null then
        raise exception 'Postgres graph store is missing';
    end if;
    if to_regclass('staging.kg_identifiers') is null then
        raise exception 'Postgres graph identifier index is missing';
    end if;

    select column_default into default_expression
    from information_schema.columns
    where table_schema = 'staging'
      and table_name = 'financial_facts'
      and column_name = 'transaction_time';
    if default_expression is not null then
        raise exception 'financial_facts.transaction_time must be explicit';
    end if;
end;
$$;

insert into raw.fetches (
    source,
    source_record_id,
    payload_sha256,
    object_uri,
    content_type,
    byte_length,
    fetched_at
) values (
    'sec',
    'contract-test',
    repeat('a', 64),
    's3://truealpha-raw/raw/sec/aa/test',
    'application/json',
    2,
    now()
);

do $$
declare
    mutation_rejected boolean := false;
begin
    begin
        update raw.fetches set byte_length = 3 where source_record_id = 'contract-test';
    exception when raise_exception then
        mutation_rejected := true;
    end;
    if not mutation_rejected then
        raise exception 'raw.fetches accepted an update';
    end if;
end;
$$;

rollback;
