-- recorded_at is an ingestion wall clock, not PostgreSQL's transaction-start
-- timestamp. Long-running source transactions can create semantic rows after
-- now(), which would invert recorded_at and transaction_time.
do $$
declare
    target record;
begin
    for target in
        select table_schema, table_name
        from information_schema.columns
        where table_schema in ('raw', 'staging', 'mart')
          and column_name = 'recorded_at'
    loop
        execute format(
            'alter table %I.%I alter column recorded_at set default clock_timestamp()',
            target.table_schema,
            target.table_name
        );
    end loop;
end $$;
