/**
 * #368: the one function production route loaders call to get a trusted
 * `AccessContext` from an incoming request. Wires together config, the DB
 * connection scoped to `app_runtime`, and session verification. Never
 * accepts a client-supplied principal/tenant/role field — the cookie is the
 * only input besides server-side config and the database.
 */

import type { NextRequest } from "next/server";
import type { AccessContext } from "@/contracts/strategyRun";
import { loadAuthConfig } from "./config";
import { withAppRuntime } from "./db";
import { getSessionAccessContext, PostgresPrincipalLookup } from "./session";

export async function getRequestAccessContext(request: NextRequest): Promise<AccessContext | null> {
  const config = loadAuthConfig();
  const cookie = request.cookies.get(config.cookieName)?.value;
  if (!cookie) return null;

  return withAppRuntime((client) =>
    getSessionAccessContext(cookie, new PostgresPrincipalLookup(client), { secret: config.secret }),
  );
}
