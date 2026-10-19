/**
 * #368: loadAuthConfig — SECRET_KEY resolution. The one security-relevant
 * behavior under test: production must refuse to fall back to the dev
 * default secret.
 *
 * Run standalone: `bun run tests/auth-config.test.ts`.
 */

import type { PoolClient } from "pg";
import { loadAuthConfig } from "../src/server/auth/config";
import { __setTestPool, withAppRuntime, withOwnerScopedRuntime } from "../src/server/auth/db";
import { withMartReadonly } from "../src/server/mart/db";
import { withOpsReader } from "../src/server/admin/ops";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function testPooledClientDisposalOnFailedCleanup() {
  const queries: string[] = [];
  const releasedWith: unknown[] = [];
  let failOnSql: string | null = null;

  const fakeClient = {
    query: async (sql: string) => {
      queries.push(sql);
      if (failOnSql && sql === failOnSql) {
        throw new Error(`simulated failure on ${sql}`);
      }
      return { rows: [] };
    },
    release: (err?: boolean | Error) => {
      releasedWith.push(err);
    },
  } as unknown as PoolClient;

  __setTestPool({
    connect: async () => fakeClient,
  });

  try {
    // Healthy role switch resets role and returns connection to pool (release(false))
    await withAppRuntime(async () => "ok");
    await withMartReadonly(async () => "ok");
    await withOpsReader(async () => "ok");
    assert(releasedWith.length === 3, `expected 3 releases, got ${releasedWith.length}`);
    assert(
      releasedWith.every((arg) => !arg),
      `healthy role switch must release(false), got ${JSON.stringify(releasedWith)}`,
    );

    // Failed reset role must evict tainted connection from shared pool (release(true))
    releasedWith.length = 0;
    failOnSql = "reset role";
    await withMartReadonly(async () => "ok");
    assert(releasedWith.length === 1 && releasedWith[0] === true, "failed reset role must call release(true)");

    // Failed rollback in owner-scoped transaction must also evict tainted connection (release(true))
    releasedWith.length = 0;
    failOnSql = "rollback";
    let txThrew = false;
    try {
      await withOwnerScopedRuntime({ tenantId: "t1", principalId: "p1" }, async () => {
        throw new Error("tx body failure");
      });
    } catch {
      txThrew = true;
    }
    assert(txThrew, "withOwnerScopedRuntime must rethrow body error");
    assert(releasedWith.length === 1 && releasedWith[0] === true, "failed rollback must call release(true)");
  } finally {
    __setTestPool(null);
  }
}

async function run() {
  const originalSecret = process.env.SECRET_KEY;
  const originalEnv = process.env.NODE_ENV;
  const originalAppEnv = process.env.APP_ENV;
  const originalExpiry = process.env.ACCESS_TOKEN_EXPIRE_MINUTES;

  try {
    // --- development: no SECRET_KEY set falls back to a dev default, does not throw ---
    delete process.env.SECRET_KEY;
    delete process.env.APP_ENV;
    (process.env as Record<string, string | undefined>).NODE_ENV = "development";
    const devConfig = loadAuthConfig();
    assert(devConfig.secret.length > 0, "dev config must still produce a usable secret");
    assert(devConfig.secureCookie === false, "dev config must set secureCookie to false");

    // --- production: no SECRET_KEY set must throw, never silently use the dev default ---
    delete process.env.SECRET_KEY;
    (process.env as Record<string, string | undefined>).NODE_ENV = "production";
    let threw = false;
    try {
      loadAuthConfig();
    } catch {
      threw = true;
    }
    assert(threw, "production with no SECRET_KEY must throw, not fall back to a hardcoded default");

    // --- deployed APP_ENV (staging/preview/production) also refuses missing SECRET_KEY ---
    delete (process.env as Record<string, string | undefined>).NODE_ENV;
    process.env.APP_ENV = "staging";
    let threwOnAppEnv = false;
    try {
      loadAuthConfig();
    } catch {
      threwOnAppEnv = true;
    }
    assert(threwOnAppEnv, "deployed APP_ENV with no SECRET_KEY must throw");

    // --- production with the dev default explicitly pasted must also throw (#447):
    // .env.example promises "refuses to start a production session on the dev
    // default", which an unset-only check does not deliver ---
    process.env.SECRET_KEY = "dev-only-insecure-secret-do-not-use-in-production-change-me";
    (process.env as Record<string, string | undefined>).NODE_ENV = "production";
    let threwOnDefault = false;
    try {
      loadAuthConfig();
    } catch {
      threwOnDefault = true;
    }
    assert(threwOnDefault, "production with the dev default explicitly set must throw, exactly like unset");

    // --- production with SECRET_KEY set: succeeds and uses it ---
    process.env.SECRET_KEY = "a-real-production-secret-at-least-32-bytes";
    (process.env as Record<string, string | undefined>).NODE_ENV = "production";
    const prodConfig = loadAuthConfig();
    assert(prodConfig.secret.length > 0, "production config with SECRET_KEY set must succeed");
    assert(prodConfig.secureCookie === true, "production config must set secureCookie to true");

    // --- staging APP_ENV without NODE_ENV=production also sets secureCookie to true ---
    delete (process.env as Record<string, string | undefined>).NODE_ENV;
    process.env.APP_ENV = "staging";
    const stagingConfig = loadAuthConfig();
    assert(stagingConfig.secureCookie === true, "staging APP_ENV must set secureCookie to true");
    (process.env as Record<string, string | undefined>).NODE_ENV = "production";

    // --- ACCESS_TOKEN_EXPIRE_MINUTES: invalid/absent value falls back to a sane default ---
    process.env.ACCESS_TOKEN_EXPIRE_MINUTES = "not-a-number";
    const fallback = loadAuthConfig();
    assert(fallback.accessTokenExpireMinutes > 0, "an invalid expiry override must fall back to a positive default");

    process.env.ACCESS_TOKEN_EXPIRE_MINUTES = "45";
    const overridden = loadAuthConfig();
    assert(overridden.accessTokenExpireMinutes === 45, `expected 45, got ${overridden.accessTokenExpireMinutes}`);
  } finally {
    if (originalSecret === undefined) delete process.env.SECRET_KEY;
    else process.env.SECRET_KEY = originalSecret;
    const env = process.env as Record<string, string | undefined>;
    if (originalEnv === undefined) delete env.NODE_ENV;
    else env.NODE_ENV = originalEnv;
    if (originalAppEnv === undefined) delete env.APP_ENV;
    else env.APP_ENV = originalAppEnv;
    if (originalExpiry === undefined) delete process.env.ACCESS_TOKEN_EXPIRE_MINUTES;
    else process.env.ACCESS_TOKEN_EXPIRE_MINUTES = originalExpiry;
  }

  await testPooledClientDisposalOnFailedCleanup();

  console.log("auth-config.test.ts: all assertions passed");
}

await run();

