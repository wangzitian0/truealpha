/**
 * #368: loadAuthConfig — SECRET_KEY resolution. The one security-relevant
 * behavior under test: production must refuse to fall back to the dev
 * default secret.
 *
 * Run standalone: `bun run tests/auth-config.test.ts`.
 */

import { loadAuthConfig } from "../src/server/auth/config";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function run() {
  const originalSecret = process.env.SECRET_KEY;
  const originalEnv = process.env.NODE_ENV;
  const originalExpiry = process.env.ACCESS_TOKEN_EXPIRE_MINUTES;

  try {
    // --- development: no SECRET_KEY set falls back to a dev default, does not throw ---
    delete process.env.SECRET_KEY;
    (process.env as Record<string, string | undefined>).NODE_ENV = "development";
    const devConfig = loadAuthConfig();
    assert(devConfig.secret.length > 0, "dev config must still produce a usable secret");

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
    if (originalExpiry === undefined) delete process.env.ACCESS_TOKEN_EXPIRE_MINUTES;
    else process.env.ACCESS_TOKEN_EXPIRE_MINUTES = originalExpiry;
  }

  console.log("auth-config.test.ts: all assertions passed");
}

run();
