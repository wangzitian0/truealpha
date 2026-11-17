"""Seed an E2E or local principal credential in `app.principals` + `app.principal_credentials`."""

from __future__ import annotations

import argparse
import os
import sys

import bcrypt
import psycopg


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed one principal credential")
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--tenant-id", default="tenant:default")
    parser.add_argument("--kind", default="member", choices=("member", "administrator", "service"))
    args = parser.parse_args()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    pw_hash = hash_password(args.password)
    with psycopg.connect(db_url) as conn:
        conn.execute("insert into app.tenants (tenant_id) values (%s) on conflict do nothing", (args.tenant_id,))
        conn.execute(
            "insert into app.principals (principal_id, tenant_id, principal_kind) values (%s, %s, %s) "
            "on conflict (principal_id) do nothing",
            (args.principal_id, args.tenant_id, args.kind),
        )
        conn.execute(
            "insert into app.principal_credentials (principal_id, email, hashed_password) values (%s, %s, %s) "
            "on conflict (principal_id) do update set email = excluded.email, hashed_password = excluded.hashed_password",
            (args.principal_id, args.email.strip().lower(), pw_hash),
        )
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
