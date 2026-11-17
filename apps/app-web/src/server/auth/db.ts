import { Pool, type PoolClient } from "pg";

export type DatabaseRole = "app_runtime" | "mart_readonly" | "app_ops_reader";

let pool: Pool | null = null;
let testPoolOverride: Pick<Pool, "connect"> | null = null;

export function __setTestPool(overridePool: Pick<Pool, "connect"> | null): void {
  testPoolOverride = overridePool;
}

export function getPool(): Pick<Pool, "connect"> {
  if (testPoolOverride) return testPoolOverride;
  if (!pool) {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
      throw new Error("DATABASE_URL is not set");
    }
    pool = new Pool({ connectionString, connectionTimeoutMillis: 5000 });
  }
  return pool;
}

export async function withRoleClient<T>(
  role: DatabaseRole,
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await getPool().connect();
  let resetFailed = false;
  try {
    await client.query(`set role ${role}`);
    return await fn(client);
  } finally {
    try {
      await client.query("reset role");
    } catch {
      resetFailed = true;
    }
    client.release(resetFailed);
  }
}

export async function withAppRuntime<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  return withRoleClient("app_runtime", fn);
}

export interface OwnerScope {
  tenantId: string;
  principalId: string;
}

export async function withOwnerScopedRuntime<T>(
  scope: OwnerScope,
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await getPool().connect();
  let rollbackFailed = false;
  try {
    await client.query("begin");
    await client.query("set local role app_runtime");
    await client.query("select set_config('truealpha.tenant_id', $1, true)", [scope.tenantId]);
    await client.query("select set_config('truealpha.principal_id', $1, true)", [scope.principalId]);
    const result = await fn(client);
    await client.query("commit");
    return result;
  } catch (error) {
    try {
      await client.query("rollback");
    } catch {
      rollbackFailed = true;
    }
    throw error;
  } finally {
    client.release(rollbackFailed);
  }
}

