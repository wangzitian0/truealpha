/**
 * #368: getSessionAccessContext — derives AccessContext only from a verified
 * session cookie, never a client-supplied field. PrincipalLookup is injected
 * (same DI pattern as tests/admin-strategy-runs.test.ts) so this runs
 * without a live Postgres.
 *
 * Run standalone: `bun run tests/auth-session.test.ts`.
 */

import { getSessionAccessContext, type PrincipalLookup } from "../src/server/auth/session";
import { signSessionToken } from "../src/server/auth/security";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const SECRET = new TextEncoder().encode("test-secret-at-least-32-bytes-long!!!!!");
const CONFIG = { secret: SECRET };

const ALWAYS_FOUND: PrincipalLookup = {
  findPrincipal: async (principalId) => ({
    principalId,
    tenantId: "tenant:truealpha",
    principalKind: "member",
  }),
};

const NEVER_FOUND: PrincipalLookup = {
  findPrincipal: async () => null,
};

async function run() {
  // --- missing cookie: denied, lookup never called ---
  {
    let called = false;
    const spy: PrincipalLookup = { findPrincipal: async (id) => { called = true; return ALWAYS_FOUND.findPrincipal(id); } };
    const ctx = await getSessionAccessContext(undefined, spy, CONFIG);
    assert(ctx === null, "missing cookie must deny");
    assert(!called, "lookup must not be called when there is no cookie");
  }

  // --- valid token, principal still exists: derives AccessContext from the session only ---
  {
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET, 30);
    const ctx = await getSessionAccessContext(token, ALWAYS_FOUND, CONFIG);
    assert(ctx !== null, "a valid token for an existing principal must derive a context");
    assert(ctx.principalId === "principal:alice", `expected principal:alice, got ${ctx.principalId}`);
    assert(ctx.tenantId === "tenant:truealpha", `expected tenant:truealpha, got ${ctx.tenantId}`);
    assert(ctx.authenticationMethod === "password", `expected password, got ${ctx.authenticationMethod}`);
  }

  // --- valid token, principal no longer exists (deleted after issuance): denied ---
  {
    const token = await signSessionToken({ sub: "principal:ghost", tenantId: "tenant:truealpha" }, SECRET, 30);
    const ctx = await getSessionAccessContext(token, NEVER_FOUND, CONFIG);
    assert(ctx === null, "a token for a principal no longer in app.principals must deny");
  }

  // --- tampered/expired token: denied before the lookup is ever consulted ---
  {
    let called = false;
    const spy: PrincipalLookup = { findPrincipal: async (id) => { called = true; return ALWAYS_FOUND.findPrincipal(id); } };
    const expired = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET, -1);
    const ctx = await getSessionAccessContext(expired, spy, CONFIG);
    assert(ctx === null, "an expired token must deny");
    assert(!called, "an invalid token must never reach the principal lookup");
  }

  // --- token tenantId claim disagrees with app.principals: denied (DB is authority, not the JWT claim) ---
  {
    const mismatched: PrincipalLookup = {
      findPrincipal: async (id) => ({ principalId: id, tenantId: "tenant:other", principalKind: "member" }),
    };
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET, 30);
    const ctx = await getSessionAccessContext(token, mismatched, CONFIG);
    assert(ctx === null, "a stale tenantId claim that disagrees with app.principals must deny, not trust the JWT");
  }

  console.log("auth-session.test.ts: all assertions passed");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
