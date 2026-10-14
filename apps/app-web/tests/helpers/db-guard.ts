/**
 * #1197: DB-backed test guard for App Web tests.
 * Enforces runtime safety invariants:
 * 1. A DB-backed test writes only when CI=true or ALLOW_CLEAR_TEST=1.
 * 2. A test never defaults DATABASE_URL to port 5432 for writes.
 * 3. `pg.Client.prototype.connect` is gated by default-deny unless `dbWritesAllowed()` is true.
 */

import { Client } from "pg";

const PG_GATE_SYMBOL = Symbol.for("truealpha.pg.write.gate");

export function dbWritesAllowed(env: NodeJS.ProcessEnv = process.env): boolean {
  const armed = env.CI === "true" || env.ALLOW_CLEAR_TEST === "1";
  return armed && Boolean(env.DATABASE_URL?.trim());
}

export function installPgWriteGate(): void {
  const proto = Client.prototype as unknown as Record<symbol, unknown> & {
    connect: (...args: unknown[]) => Promise<unknown>;
  };
  if (proto[PG_GATE_SYMBOL]) {
    return;
  }
  const realConnect = proto.connect;
  proto.connect = async function gatedConnect(this: Client, ...args: unknown[]): Promise<unknown> {
    if (!dbWritesAllowed()) {
      throw new Error(
        "DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1 and DATABASE_URL to allow database access",
      );
    }
    return realConnect.apply(this, args);
  };
  proto[PG_GATE_SYMBOL] = true;
}

installPgWriteGate();

export function requireDb(testName: string, options: { write?: boolean } = {}): string | null {
  const isCI = process.env.CI === "true";
  const allowClear = process.env.ALLOW_CLEAR_TEST === "1";
  const requireRuntime = Boolean(process.env.TRUEALPHA_REQUIRE_RUNTIME);
  const write = options.write ?? true;

  if (write && !isCI && !allowClear) {
    console.log(`${testName}: DB write tests require CI=true or ALLOW_CLEAR_TEST=1 — SKIP`);
    return null;
  }

  const databaseUrl = process.env.DATABASE_URL?.trim();
  if (!databaseUrl) {
    if (requireRuntime) {
      throw new Error(`[${testName}] DATABASE_URL is required when TRUEALPHA_REQUIRE_RUNTIME is set`);
    }
    console.log(`${testName}: DATABASE_URL unset and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP`);
    return null;
  }

  return databaseUrl;
}

