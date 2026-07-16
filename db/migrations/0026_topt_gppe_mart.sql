-- Queryable, append-only GPPE and three-tier valuation outputs for TOPT runs.

create table if not exists mart.topt_gppe_results (
    result_id              text primary key check (result_id ~ '^topt-gppe-result:[0-9a-f]{64}$'),
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    run_id                 text not null references raw.capture_runs(run_id),
    issuer_id              text not null,
    cutoff                 timestamptz not null,
    availability           text not null check (availability in ('available', 'unavailable')),
    gross_profit           numeric,
    employee_headcount     bigint,
    gppe                    numeric,
    tier                    text check (tier in ('traditional', 'tech', 'large_model_native')),
    target_ps_lower         numeric,
    target_ps_upper         numeric,
    confidence              numeric not null check (confidence between 0 and 1),
    reason_codes            text[] not null,
    payload                 jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at              timestamptz not null default now(),
    unique (run_id, issuer_id),
    check (
        (availability = 'available' and gppe is not null and tier is not null
            and target_ps_lower is not null and target_ps_upper is not null
            and target_ps_lower < target_ps_upper and cardinality(reason_codes) = 0)
        or
        (availability = 'unavailable' and gppe is null and tier is null
            and target_ps_lower is null and target_ps_upper is null
            and confidence = 0 and cardinality(reason_codes) > 0)
    )
);

drop trigger if exists reject_mutation on mart.topt_gppe_results;
create trigger reject_mutation
before update or delete on mart.topt_gppe_results
for each row execute function raw.reject_capture_control_mutation();
