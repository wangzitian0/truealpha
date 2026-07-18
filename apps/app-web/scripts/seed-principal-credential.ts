#!/usr/bin/env bun
/**
 * #368: administrator-only credential seeding. There is no self-serve
 * registration endpoint anywhere in app-web — this script is the only way a
 * login credential is created, for a small named set of users (the owner
 * plus invited friends).
 *
 * Requires an existing `app.principals` row (created by #229's tooling /
 * direct SQL against app.principals + app.tenant_memberships +
 * app.entitlement_grants) — this script only ever adds/rotates the
 * credential for a principal that already exists. It never creates a
 * principal, tenant, or grant.
 *
 * Usage:
 *   DATABASE_URL=postgresql://... bun run scripts/seed-principal-credential.ts \
 *     --principal-id principal:friend-alice --email alice@example.com --password 'a real passphrase'
 */

import { Pool } from "pg";
import { hashPassword } from "../src/server/auth/security";

function parseArgs(argv: string[]): Record<string, string> {
  const args: Record<string, string> = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const value = argv[i + 1];
      if (value === undefined || value.startsWith("--")) {
        throw new Error(`--${key} requires a value`);
      }
      args[key] = value;
      i += 1;
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const principalId = args["principal-id"];
  const email = args.email;
  const password = args.password;

  if (!principalId || !email || !password) {
    console.error("Usage: bun run scripts/seed-principal-credential.ts --principal-id <id> --email <email> --password <password>");
    process.exit(1);
  }
  if (password.length < 12) {
    console.error("Refusing a password shorter than 12 characters.");
    process.exit(1);
  }

  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    console.error("DATABASE_URL is not set.");
    process.exit(1);
  }

  const pool = new Pool({ connectionString });
  const client = await pool.connect();
  try {
    await client.query("set role app_runtime");

    const principal = await client.query("select principal_id, tenant_id from app.principals where principal_id = $1", [
      principalId,
    ]);
    if (principal.rows.length === 0) {
      console.error(
        `No app.principals row for ${principalId}. Create the principal (and its tenant membership / entitlement grants) first — this script only seeds credentials for an existing principal.`,
      );
      process.exit(1);
    }

    const hashedPassword = await hashPassword(password);
    await client.query(
      `insert into app.principal_credentials (principal_id, email, hashed_password)
       values ($1, $2, $3)
       on conflict (principal_id) do update set email = excluded.email, hashed_password = excluded.hashed_password`,
      [principalId, email, hashedPassword],
    );

    console.log(`Seeded credentials for ${principalId} (${email}).`);
  } finally {
    await client.query("reset role").catch(() => {});
    client.release();
    await pool.end();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
