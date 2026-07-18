/**
 * #368: resolveLogin — verifies credentials against an injected repository
 * (no live Postgres). The key security property under test: an unknown
 * email and a known email with the wrong password must return the exact
 * same outcome shape, so a client cannot enumerate which emails have
 * accounts.
 *
 * Run standalone: `bun run tests/auth-login.test.ts`.
 */

import { resolveLogin, type CredentialsRepository } from "../src/server/auth/credentials";
import { hashPassword } from "../src/server/auth/security";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function run() {
  const aliceHash = await hashPassword("alice-password-123");
  const repo: CredentialsRepository = {
    findByEmail: async (email) =>
      email === "alice@example.com"
        ? { principalId: "principal:alice", tenantId: "tenant:truealpha", hashedPassword: aliceHash }
        : null,
  };

  // --- correct email + password: success, carries principal/tenant ---
  {
    const outcome = await resolveLogin("alice@example.com", "alice-password-123", repo);
    assert(outcome.kind === "success", `expected success, got ${outcome.kind}`);
    assert(outcome.principalId === "principal:alice", `expected principal:alice, got ${outcome.principalId}`);
    assert(outcome.tenantId === "tenant:truealpha", `expected tenant:truealpha, got ${outcome.tenantId}`);
  }

  // --- correct email, wrong password: invalid_credentials ---
  const wrongPassword = await resolveLogin("alice@example.com", "not-the-password", repo);
  assert(wrongPassword.kind === "invalid_credentials", `expected invalid_credentials, got ${wrongPassword.kind}`);

  // --- unknown email: invalid_credentials, same shape as wrong password (no enumeration) ---
  const unknownEmail = await resolveLogin("nobody@example.com", "anything", repo);
  assert(unknownEmail.kind === "invalid_credentials", `expected invalid_credentials, got ${unknownEmail.kind}`);
  assert(
    Object.keys(unknownEmail).length === Object.keys(wrongPassword).length,
    "unknown-email and wrong-password outcomes must be structurally identical (no enumeration signal)",
  );

  // --- email lookup is case-insensitive and trims whitespace ---
  {
    const outcome = await resolveLogin("  Alice@Example.com  ", "alice-password-123", repo);
    assert(outcome.kind === "success", `expected case/whitespace-insensitive match to succeed, got ${outcome.kind}`);
  }

  console.log("auth-login.test.ts: all assertions passed");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
