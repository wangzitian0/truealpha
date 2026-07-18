-- Owner-scoped conversation persistence + clarification tokens — see #396
-- (#225's discovery, #371's /conversations route). Mirrors app.principal_credentials
-- (0029) and app.private_research_objects (0022): RLS-isolated per owner via the
-- same transaction-local truealpha.tenant_id / truealpha.principal_id GUCs.

create table if not exists app.conversations (
    conversation_id    text primary key check (length(conversation_id) > 0),
    tenant_id          text not null references app.tenants (tenant_id),
    owner_principal_id text not null references app.principals (principal_id),
    created_at         timestamptz not null default now()
);

alter table app.conversations enable row level security;
alter table app.conversations force row level security;

drop policy if exists conversations_owner_isolation on app.conversations;
create policy conversations_owner_isolation on app.conversations
    for all
    using (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    )
    with check (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    );

create table if not exists app.conversation_messages (
    message_id         text primary key check (length(message_id) > 0),
    conversation_id     text not null references app.conversations (conversation_id),
    tenant_id           text not null references app.tenants (tenant_id),
    owner_principal_id  text not null references app.principals (principal_id),
    role                text not null check (role in ('user', 'assistant')),
    content             text not null check (length(content) > 0),
    -- Nullable: a user's own prompt has no outcome until it is processed —
    -- outcome is assistant-side semantics (#225's typed state machine).
    -- An assistant-role row should carry one; that is an application-layer
    -- expectation (#46's orchestration), not enforced here as a CHECK,
    -- since nothing in this repo produces assistant replies yet.
    outcome             text check (outcome in (
        'result', 'clarification_required', 'unavailable', 'unsupported',
        'denied', 'rate_limited', 'invalid'
    )),
    created_at          timestamptz not null default now()
);

create index if not exists idx_conversation_messages_conversation
    on app.conversation_messages (conversation_id, created_at);

alter table app.conversation_messages enable row level security;
alter table app.conversation_messages force row level security;

drop policy if exists conversation_messages_owner_isolation on app.conversation_messages;
create policy conversation_messages_owner_isolation on app.conversation_messages
    for all
    using (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    )
    with check (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    );

-- Messages are append-only: no edit, no delete. Reuses app.reject_mutation() from 0022.
drop trigger if exists trg_conversation_messages_append_only on app.conversation_messages;
create trigger trg_conversation_messages_append_only
before update or delete on app.conversation_messages
for each row execute function app.reject_mutation();

create table if not exists app.clarification_tokens (
    token_id                text primary key check (length(token_id) > 0),
    conversation_id          text not null references app.conversations (conversation_id),
    tenant_id                text not null references app.tenants (tenant_id),
    owner_principal_id       text not null references app.principals (principal_id),
    originating_message_id   text not null references app.conversation_messages (message_id),
    requested_fields         text[] not null check (array_length(requested_fields, 1) > 0),
    candidate_choices        text[] not null default array[]::text[],
    expires_at               timestamptz not null,
    redeemed_at              timestamptz,
    created_at               timestamptz not null default now(),
    check (expires_at > created_at),
    check (redeemed_at is null or redeemed_at >= created_at)
);

alter table app.clarification_tokens enable row level security;
alter table app.clarification_tokens force row level security;

drop policy if exists clarification_tokens_owner_isolation on app.clarification_tokens;
create policy clarification_tokens_owner_isolation on app.clarification_tokens
    for all
    using (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    )
    with check (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    );

-- Single redemption is enforced at the repository layer via an atomic
-- conditional UPDATE (`WHERE redeemed_at IS NULL AND expires_at > now()`),
-- not a trigger: this guards against a *second* redemption attempt racing
-- the first, which a trigger checking OLD.redeemed_at would also catch, but
-- the WHERE-guarded UPDATE additionally makes the "0 rows affected" case
-- the single signal a repository needs, with no separate error path.
create or replace function app.reject_clarification_token_field_tamper()
returns trigger language plpgsql as $$
begin
    if new.conversation_id is distinct from old.conversation_id
        or new.tenant_id is distinct from old.tenant_id
        or new.owner_principal_id is distinct from old.owner_principal_id
        or new.originating_message_id is distinct from old.originating_message_id
        or new.requested_fields is distinct from old.requested_fields
        or new.candidate_choices is distinct from old.candidate_choices
        or new.expires_at is distinct from old.expires_at
        or new.created_at is distinct from old.created_at
    then
        raise exception 'clarification_tokens: only redeemed_at may change after creation';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_clarification_tokens_guard_update on app.clarification_tokens;
create trigger trg_clarification_tokens_guard_update
before update on app.clarification_tokens
for each row execute function app.reject_clarification_token_field_tamper();

create table if not exists app.research_gap_requests (
    gap_request_id      text primary key check (length(gap_request_id) > 0),
    tenant_id            text not null references app.tenants (tenant_id),
    owner_principal_id   text not null references app.principals (principal_id),
    conversation_id      text references app.conversations (conversation_id),
    prompt_text          text not null check (length(prompt_text) > 0),
    created_at           timestamptz not null default now()
);

alter table app.research_gap_requests enable row level security;
alter table app.research_gap_requests force row level security;

drop policy if exists research_gap_requests_owner_isolation on app.research_gap_requests;
create policy research_gap_requests_owner_isolation on app.research_gap_requests
    for all
    using (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    )
    with check (
        tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
        and owner_principal_id = nullif(current_setting('truealpha.principal_id', true), '')
    );

-- Content-free: no consent flag or declined row exists. A row's mere
-- existence is the consented case (#225); a declined suggestion never
-- reaches this table at all (repository layer never calls insert for it).
drop trigger if exists trg_research_gap_requests_append_only on app.research_gap_requests;
create trigger trg_research_gap_requests_append_only
before update or delete on app.research_gap_requests
for each row execute function app.reject_mutation();

-- Administrator non-content audit projection: counts/timestamps only, ever
-- exposed to an administrator, never message/prompt content. Mirrors
-- app.access_audit_metadata's shape (0022): security_barrier + an inline
-- administrator check, granted only to app_audit_reader.
create or replace view app.conversation_audit_metadata
with (security_barrier = true)
as
select
    c.tenant_id,
    c.conversation_id,
    c.owner_principal_id,
    c.created_at as conversation_created_at,
    count(m.message_id) as message_count,
    max(m.created_at) as last_message_at
from app.conversations c
left join app.conversation_messages m on m.conversation_id = c.conversation_id
where c.tenant_id = nullif(current_setting('truealpha.tenant_id', true), '')
  and exists (
    select 1
    from app.principals as reader
    where reader.principal_id = nullif(current_setting('truealpha.principal_id', true), '')
      and reader.principal_kind = 'administrator'
)
group by c.tenant_id, c.conversation_id, c.owner_principal_id, c.created_at;

grant select, insert on app.conversations, app.conversation_messages, app.research_gap_requests to app_runtime;
grant select, insert, update (redeemed_at) on app.clarification_tokens to app_runtime;
revoke select on app.conversation_audit_metadata from app_runtime;
grant select on app.conversation_audit_metadata to app_audit_reader;
