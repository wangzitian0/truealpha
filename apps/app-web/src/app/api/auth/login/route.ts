/**
 * #368: POST /api/auth/login — the only credential-verifying endpoint.
 * There is no /api/auth/register anywhere in this app: credentials are
 * seeded by an administrator (scripts/seed-principal-credential.ts).
 */

import { NextResponse, type NextRequest } from "next/server";
import { loadAuthConfig } from "@/server/auth/config";
import { PostgresCredentialsRepository, resolveLogin } from "@/server/auth/credentials";
import { withAppRuntime } from "@/server/auth/db";
import { loginRateLimiter } from "@/server/auth/rate-limit";
import { signSessionToken } from "@/server/auth/security";

export const dynamic = "force-dynamic";

function clientIp(request: NextRequest): string {
  // Behind a reverse proxy in Staging/Production; falls back to a shared
  // bucket for direct/local connections rather than throwing.
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const ip = clientIp(request);
  if (!loginRateLimiter.attempt(ip)) {
    return NextResponse.json({ error: "too_many_attempts" }, { status: 429 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }
  if (
    typeof body !== "object" ||
    body === null ||
    typeof (body as Record<string, unknown>).email !== "string" ||
    typeof (body as Record<string, unknown>).password !== "string"
  ) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }
  const { email, password } = body as { email: string; password: string };

  const outcome = await withAppRuntime((client) => resolveLogin(email, password, new PostgresCredentialsRepository(client)));
  if (outcome.kind !== "success") {
    return NextResponse.json({ error: "invalid_credentials" }, { status: 401 });
  }

  const config = loadAuthConfig();
  const token = await signSessionToken(
    { sub: outcome.principalId, tenantId: outcome.tenantId },
    config.secret,
    config.accessTokenExpireMinutes,
  );

  const response = NextResponse.json({ ok: true });
  response.cookies.set(config.cookieName, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: config.accessTokenExpireMinutes * 60,
  });
  return response;
}
