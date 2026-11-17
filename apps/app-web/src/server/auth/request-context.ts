/**
 * Derives trusted `SessionPrincipal` (`AccessContext` + `principalKind`) for Server Components
 * and Route Handlers from the verified session cookie via `cache()`.
 */

import { cookies } from "next/headers";
import { cache } from "react";
import { loadAuthConfig } from "./config";
import { withAppRuntime } from "./db";
import { getSessionPrincipal, PostgresPrincipalLookup, type SessionPrincipal } from "./session";

export const getServerPrincipal = cache(async (): Promise<SessionPrincipal | null> => {
  const config = loadAuthConfig();
  const cookieStore = await cookies();
  const cookie = cookieStore.get(config.cookieName)?.value;
  if (!cookie) return null;

  return withAppRuntime((client) =>
    getSessionPrincipal(cookie, new PostgresPrincipalLookup(client), { secret: config.secret }),
  );
});
