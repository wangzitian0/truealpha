import bcrypt from "bcryptjs";
import { SignJWT, jwtVerify } from "jose";

const BCRYPT_SALT_ROUNDS = 12;

export async function hashPassword(password: string): Promise<string> {
  return bcrypt.hash(password, BCRYPT_SALT_ROUNDS);
}

export async function verifyPassword(password: string, hashedPassword: string): Promise<boolean> {
  return bcrypt.compare(password, hashedPassword);
}

export interface SessionClaims {
  sub: string;
  tenantId: string;
}

export interface VerifiedSession extends SessionClaims {
  issuedAt: Date;
  expiresAt: Date;
}

export async function signSessionToken(
  claims: SessionClaims,
  secret: Uint8Array,
  expiresInMinutes: number,
): Promise<string> {
  const issuedAt = new Date();
  const expiresAt = new Date(issuedAt.getTime() + expiresInMinutes * 60_000);
  return new SignJWT({ tenantId: claims.tenantId })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(claims.sub)
    .setIssuedAt(issuedAt)
    .setExpirationTime(expiresAt)
    .sign(secret);
}

export async function verifySessionToken(token: string, secret: Uint8Array): Promise<VerifiedSession | null> {
  try {
    const { payload } = await jwtVerify(token, secret, { algorithms: ["HS256"] });
    const { sub, tenantId, iat, exp } = payload;
    if (typeof sub !== "string" || sub.length === 0) return null;
    if (typeof tenantId !== "string" || tenantId.length === 0) return null;
    if (typeof iat !== "number" || typeof exp !== "number") return null;
    return {
      sub,
      tenantId,
      issuedAt: new Date(iat * 1000),
      expiresAt: new Date(exp * 1000),
    };
  } catch {
    return null;
  }
}
