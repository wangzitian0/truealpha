-- #70 scope 2 / #735 B1 (init.md §9): every model call the loop makes is an append-only
-- invocation record. It binds the provider, the model, the instruction and schema digests,
-- the exact request and response digests, the token cost and the decision, so that
--   * a replay reads the stored decision and never calls the provider again,
--   * a changed instruction, schema or model is a NEW invocation (a new vintage), and
--   * a published value can be traced from its fact row (`evidence_ref` names the
--     invocation id) to the exact answer the model gave.
-- The request and the response are kept as jsonb because the vendor's answer is the
-- evidence and the instructions are part of the identity; a non-JSON vendor body (an HTML
-- error page) leaves `response` null and survives through `response_sha256` plus the
-- ledger's error text. Vendor errors are recorded (status_code >= 400) and never replayed.
create table if not exists staging.model_invocations (
    id                 bigint generated always as identity primary key,
    invocation_id      text not null unique
        check (invocation_id ~ '^model-invocation:[0-9a-f]{64}$'),
    provider           text not null check (provider <> ''),
    model              text not null check (model <> ''),
    base_url_host      text not null check (base_url_host <> ''),
    standard           text not null check (standard <> ''),
    subject_cik        integer not null check (subject_cik > 0),
    accession          text not null check (accession <> ''),
    prompt_version     text not null check (prompt_version <> ''),
    prompt_sha256      text not null check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    schema_sha256      text not null check (schema_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256     text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_sha256    text check (response_sha256 is null or response_sha256 ~ '^[0-9a-f]{64}$'),
    status_code        integer,
    prompt_tokens      integer check (prompt_tokens is null or prompt_tokens >= 0),
    completion_tokens  integer check (completion_tokens is null or completion_tokens >= 0),
    cost               numeric not null default 0 check (cost >= 0),
    decision           jsonb not null,
    request            jsonb not null,
    response           jsonb,
    started_at         timestamptz not null,
    completed_at       timestamptz not null,
    recorded_at        timestamptz not null default now(),
    check (completed_at >= started_at)
);
comment on table staging.model_invocations is
    '#70/#735: append-only record of every filing-extraction model call — identity (provider, model, prompt/schema/request/response digests), cost and decision. Replay reads it; nothing updates it.';
create index if not exists ix_model_invocations_subject
    on staging.model_invocations (subject_cik, accession, prompt_sha256, model, id desc);
drop trigger if exists reject_mutation on staging.model_invocations;
create trigger reject_mutation
before update or delete on staging.model_invocations
for each row execute function raw.reject_mutation();
