begin;

do $$
declare
    required_column_count integer;
    required_trigger_count integer;
begin
    if to_regclass('staging.headcount_extraction_invocations') is null then
        raise exception 'headcount extraction invocation repository is missing';
    end if;
    if to_regclass('staging.headcount_facts') is null then
        raise exception 'headcount fact projection is missing';
    end if;

    select count(*) into required_column_count
    from information_schema.columns
    where table_schema = 'staging'
      and (
        (
            table_name = 'headcount_extraction_invocations'
            and column_name in (
                'source_document_record_id',
                'document_sha256',
                'model_revision_id',
                'extraction_template_id',
                'input_sha256',
                'response_sha256',
                'semantic_payload_sha256',
                'started_at',
                'completed_at',
                'recorded_at'
            )
            and is_nullable = 'NO'
        )
        or (
            table_name = 'headcount_facts'
            and column_name in (
                'availability',
                'valid_period_end',
                'transaction_time',
                'recorded_at',
                'confidence',
                'review_status',
                'evidence_spans',
                'raw_ref'
            )
            and is_nullable = 'NO'
        )
      );
    if required_column_count <> 18 then
        raise exception 'headcount PIT, invocation, confidence, span, or lineage columns are incomplete';
    end if;

    select count(*) into required_trigger_count
    from pg_trigger
    where not tgisinternal
      and tgname in (
        'trg_headcount_invocations_validate',
        'trg_headcount_invocations_append_only',
        'trg_headcount_facts_validate',
        'trg_headcount_facts_append_only'
      )
      and tgrelid in (
        'staging.headcount_extraction_invocations'::regclass,
        'staging.headcount_facts'::regclass
      );
    if required_trigger_count <> 4 then
        raise exception 'headcount validation or append-only triggers are incomplete';
    end if;
    if to_regclass('staging.idx_headcount_invocation_document') is null
       or to_regclass('staging.idx_headcount_facts_pit') is null then
        raise exception 'headcount replay or PIT indexes are missing';
    end if;
end;
$$;

rollback;
