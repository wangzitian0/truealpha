import { verifyPassword } from "./security";

export interface CredentialRecord {
  principalId: string;
  tenantId: string;
  hashedPassword: string;
}

export interface CredentialsRepository {
  findByEmail(email: string): Promise<CredentialRecord | null>;
}

export type LoginOutcome =
  | { kind: "success"; principalId: string; tenantId: string }
  | { kind: "invalid_credentials" };

const DUMMY_BCRYPT_HASH = "$2a$12$CwTycUXWue0Thq9StjUM0uJ8m8p1EbSN4DDzTRAeixmwapDGO2LbG";

export async function resolveLogin(
  email: string,
  password: string,
  repo: CredentialsRepository,
): Promise<LoginOutcome> {
  const normalizedEmail = email.trim().toLowerCase();
  const record = await repo.findByEmail(normalizedEmail);
  const passwordMatches = await verifyPassword(password, record?.hashedPassword ?? DUMMY_BCRYPT_HASH);

  if (!record || !passwordMatches) {
    return { kind: "invalid_credentials" };
  }
  return { kind: "success", principalId: record.principalId, tenantId: record.tenantId };
}

export interface QueryExecutor {
  query<Row extends Record<string, unknown>>(text: string, params?: unknown[]): Promise<{ rows: Row[] }>;
}

export class PostgresCredentialsRepository implements CredentialsRepository {
  constructor(private readonly db: QueryExecutor) {}

  async findByEmail(email: string): Promise<CredentialRecord | null> {
    const result = await this.db.query<{ principal_id: string; tenant_id: string; hashed_password: string }>(
      `select c.principal_id, p.tenant_id, c.hashed_password
       from app.principal_credentials c
       join app.principals p on p.principal_id = c.principal_id
       where lower(c.email) = lower($1)`,
      [email],
    );
    const row = result.rows[0];
    if (!row) return null;
    return { principalId: row.principal_id, tenantId: row.tenant_id, hashedPassword: row.hashed_password };
  }
}
