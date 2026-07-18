/** #368: POST /api/auth/logout — clears the session cookie. No server-side
 * token revocation list exists yet (short-lived-enough sessions + this being
 * a small named-user deployment, not a public SaaS, made that an accepted
 * v1 gap rather than blocking scope). */

import { NextResponse } from "next/server";
import { loadAuthConfig } from "@/server/auth/config";

export const dynamic = "force-dynamic";

export async function POST(): Promise<NextResponse> {
  const config = loadAuthConfig();
  const response = NextResponse.json({ ok: true });
  response.cookies.set(config.cookieName, "", {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 0,
  });
  return response;
}
