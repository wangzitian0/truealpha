/**
 * #371: getSessionPrincipal — same session verification as #368's
 * getSessionAccessContext, but also surfaces `principalKind` so route
 * groups can gate /admin vs /research. Administrator vs normal-user
 * routing must be driven by `app.principals.principal_kind`, never client
 * input (#368 acceptance criterion, exercised here at the session layer).
 *
 * Run standalone: `bun run tests/auth-session-principal.test.ts`.
 */

import { getSessionPrincipal, type PrincipalLookup } from "../src/server/auth/session";
import { signSessionToken } from "../src/server/auth/security";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const SECRET = new TextEncoder().encode("test-secret-at-least-32-bytes-long!!!!!");
const CONFIG = { secret: SECRET };

async function run() {
  // --- member principal: surfaces principalKind "member" alongside the context ---
  {
    const lookup: PrincipalLookup = {
      findPrincipal: async (id) => ({ principalId: id, tenantId: "tenant:truealpha", principalKind: "member" }),
    };
    const token = await signSessionToken({ sub: "principal:alice", tenantId: "tenant:truealpha" }, SECRET, 30);
    const resolved = await getSessionPrincipal(token, lookup, CONFIG);
    assert(resolved !== null, "a valid session for an existing principal must resolve");
    assert(resolved.principalKind === "member", `expected member, got ${resolved.principalKind}`);
    assert(resolved.context.principalId === "principal:alice", "context must carry the same principalId");
  }

  // --- administrator principal: surfaces principalKind "administrator" ---
  {
    const lookup: PrincipalLookup = {
      findPrincipal: async (id) => ({ principalId: id, tenantId: "tenant:truealpha", principalKind: "administrator" }),
    };
    const token = await signSessionToken({ sub: "principal:owner", tenantId: "tenant:truealpha" }, SECRET, 30);
    const resolved = await getSessionPrincipal(token, lookup, CONFIG);
    assert(resolved !== null, "a valid session for an administrator must resolve");
    assert(resolved.principalKind === "administrator", `expected administrator, got ${resolved.principalKind}`);
  }

  // --- missing/invalid/expired/unresolvable cases all deny the same as getSessionAccessContext ---
  {
    const lookup: PrincipalLookup = { findPrincipal: async () => null };
    const resolved = await getSessionPrincipal(undefined, lookup, CONFIG);
    assert(resolved === null, "missing cookie must deny");
  }
  {
    const lookup: PrincipalLookup = { findPrincipal: async () => null };
    const token = await signSessionToken({ sub: "principal:ghost", tenantId: "tenant:truealpha" }, SECRET, 30);
    const resolved = await getSessionPrincipal(token, lookup, CONFIG);
    assert(resolved === null, "a principal no longer in app.principals must deny");
  }

  console.log("auth-session-principal.test.ts: all assertions passed");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
