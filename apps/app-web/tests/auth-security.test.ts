/**
 * #368: bcrypt password hashing + HS256 JWT session tokens — pure crypto,
 * no DB, no Next.js. Mirrors the finance_report identity scheme
 * (apps/backend/src/identity/extension/security.py) ported to TypeScript.
 *
 * Run standalone: `bun run tests/auth-security.test.ts`.
 */

import {
  hashPassword,
  verifyPassword,
  signSessionToken,
  verifySessionToken,
} from "../src/server/auth/security";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const SECRET_A = new TextEncoder().encode("test-secret-a-at-least-32-bytes-long!!");
const SECRET_B = new TextEncoder().encode("test-secret-b-at-least-32-bytes-long!!");

async function run() {
  // --- bcrypt: correct password verifies ---
  {
    const hash = await hashPassword("correct horse battery staple");
    assert(await verifyPassword("correct horse battery staple", hash), "correct password must verify");
  }

  // --- bcrypt: wrong password is rejected ---
  {
    const hash = await hashPassword("correct horse battery staple");
    assert(!(await verifyPassword("wrong password", hash)), "wrong password must not verify");
  }

  // --- bcrypt: same password hashes differently each time (per-password salt) ---
  {
    const hashOne = await hashPassword("same password");
    const hashTwo = await hashPassword("same password");
    assert(hashOne !== hashTwo, "two hashes of the same password must differ (salted)");
    assert(await verifyPassword("same password", hashOne), "hashOne must still verify");
    assert(await verifyPassword("same password", hashTwo), "hashTwo must still verify");
  }

  // --- JWT: sign then verify round-trips claims ---
  {
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET_A, 30);
    const verified = await verifySessionToken(token, SECRET_A);
    assert(verified !== null, "a freshly signed token must verify");
    assert(verified.sub === "principal:alice", `expected sub principal:alice, got ${verified.sub}`);
    assert(verified.tenantId === "tenant:truealpha", `expected tenantId tenant:truealpha, got ${verified.tenantId}`);
    assert(verified.expiresAt.getTime() > verified.issuedAt.getTime(), "expiresAt must be after issuedAt");
  }

  // --- JWT: a token signed with a different secret is rejected ---
  {
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET_A, 30);
    const verified = await verifySessionToken(token, SECRET_B);
    assert(verified === null, "a token verified against the wrong secret must be rejected");
  }

  // --- JWT: an expired token is rejected ---
  {
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET_A, -1);
    const verified = await verifySessionToken(token, SECRET_A);
    assert(verified === null, "an already-expired token must be rejected");
  }

  // --- JWT: garbage input is rejected, not thrown ---
  {
    const verified = await verifySessionToken("not.a.jwt", SECRET_A);
    assert(verified === null, "malformed token input must return null, not throw");
  }

  console.log("auth-security.test.ts: all assertions passed");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
