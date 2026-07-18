-- Login front door for #229's identity backend — see #368.
-- Adds the one missing table: password credentials bound to an existing
-- app.principals row. This table intentionally does NOT get the append-only
-- reject_mutation trigger used elsewhere in the app schema (0022) — unlike
-- identity/policy/audit *events*, a credential legitimately needs an
-- in-place password rotation later; it stays a plain mutable table.
--
-- v1 has no self-serve registration path: rows are seeded by an
-- administrator (a one-off script), binding a credential to a (tenant,
-- principal) that already exists. This migration never creates a principal,
-- tenant, grant, or principal_kind — only credentials for one.

create table if not exists app.principal_credentials (
    principal_id    text primary key references app.principals (principal_id),
    email           text not null check (length(email) > 0),
    hashed_password text not null check (length(hashed_password) > 0),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Case-insensitive uniqueness: "a@b.com" and "A@B.com" are the same login.
create unique index if not exists idx_principal_credentials_email_lower
    on app.principal_credentials (lower(email));

create or replace function app.touch_principal_credentials_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists trg_principal_credentials_touch on app.principal_credentials;
create trigger trg_principal_credentials_touch
before update on app.principal_credentials
for each row execute function app.touch_principal_credentials_updated_at();

-- app_runtime (0022) is the trusted server-side role app-web's backend
-- connects as. Login itself runs before any tenant/principal GUC is set (it
-- is what *establishes* that context), so these grants are plain
-- schema-level selects/writes, not RLS-scoped. principal_credentials carries
-- no research content, so it needs no RLS policy of its own.
grant select, insert, update on app.principal_credentials to app_runtime;
grant select on app.principals, app.tenants to app_runtime;
