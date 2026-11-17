import { NextResponse } from "next/server";
import { loadAuthConfig } from "@/server/auth/config";

export const dynamic = "force-dynamic";

export async function POST(): Promise<NextResponse> {
  const config = loadAuthConfig();
  const response = NextResponse.json({ ok: true });
  response.cookies.set(config.cookieName, "", {
    httpOnly: true,
    sameSite: "lax",
    secure: config.secureCookie,
    path: "/",
    maxAge: 0,
  });
  return response;
}
