/**
 * #1197: DB-backed test guard for App Web tests.
 * Enforces runtime safety invariants:
 * 1. A DB-backed test writes only when CI=true or ALLOW_CLEAR_TEST=1.
 * 2. A test never defaults DATABASE_URL to port 5432 for writes.
 */

export function requireDb(testName: string, options: { write?: boolean } = {}): string | null {
  const isCI = process.env.CI === "true";
  const allowClear = process.env.ALLOW_CLEAR_TEST === "1";
  const requireRuntime = Boolean(process.env.TRUEALPHA_REQUIRE_RUNTIME);

  if (options.write && !isCI && !allowClear) {
    console.log(`${testName}: DB write tests require CI=true or ALLOW_CLEAR_TEST=1 — SKIP`);
    return null;
  }

  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    if (requireRuntime) {
      throw new Error(`[${testName}] DATABASE_URL is required when TRUEALPHA_REQUIRE_RUNTIME is set`);
    }
    console.log(`${testName}: DATABASE_URL unset and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP`);
    return null;
  }

  return databaseUrl;
}
