/**
 * #368/#371: the functions production route loaders call to get a trusted
 * `AccessContext` (and, for #371's route-group gating, `principalKind`)
 * from the current request. Wires together config, the DB connection scoped
 * to `app_runtime`, and session verification. Never accepts a
 * client-supplied principal/tenant/role field — the cookie is the only
 * input besides server-side config and the database.
 */

import type { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { cache } from "react";
import type { AccessContext } from "@/contracts/strategyRun";
import { loadAuthConfig } from "./config";
import { withAppRuntime } from "./db";
import { getSessionAccessContext, getSessionPrincipal, PostgresPrincipalLookup, type SessionPrincipal } from "./session";

/** For Next.js Route Handlers, which receive a `NextRequest` directly. */
export async function getRequestAccessContext(request: NextRequest): Promise<AccessContext | null> {
  const config = loadAuthConfig();
  const cookie = request.cookies.get(config.cookieName)?.value;
  if (!cookie) return null;

  return withAppRuntime((client) =>
    getSessionAccessContext(cookie, new PostgresPrincipalLookup(client), { secret: config.secret }),
  );
}

/** For Route Handlers that need role too (e.g. gating an admin-only API route). */
export async function getRequestPrincipal(request: NextRequest): Promise<SessionPrincipal | null> {
  const config = loadAuthConfig();
  const cookie = request.cookies.get(config.cookieName)?.value;
  if (!cookie) return null;

  return withAppRuntime((client) =>
    getSessionPrincipal(cookie, new PostgresPrincipalLookup(client), { secret: config.secret }),
  );
}

/** For Server Components / route-group layouts, which have no `NextRequest`
 * and read the request cookie jar via `next/headers` instead. Next.js 15's
 * `cookies()` is async.
 *
 * Wrapped in React's `cache()`: a layout and its page both call this on the
 * same request (each re-authorizing independently, by design — #368/#371),
 * which would otherwise be two DB round trips per page load. `cache()`
 * dedupes calls within one render pass; it is a per-request memo, not a
 * cross-request cache, so it cannot leak one visitor's session into another
 * request. */
export const getServerPrincipal = cache(async (): Promise<SessionPrincipal | null> => {
  const config = loadAuthConfig();
  const cookieStore = await cookies();
  const cookie = cookieStore.get(config.cookieName)?.value;
  if (!cookie) return null;

  return withAppRuntime((client) =>
    getSessionPrincipal(cookie, new PostgresPrincipalLookup(client), { secret: config.secret }),
  );
});
