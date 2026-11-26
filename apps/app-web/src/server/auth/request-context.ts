import { cache } from "react";
import { cookies, headers } from "next/headers";
import type { RequestOpts } from "@/client/api";
import { resolveSessionPrincipal } from "./session";

export interface ServerPrincipal {
  principalKind: "member" | "administrator" | "service";
  context: { principalId: string; tenantId: string; contextId: string };
  requestOpts: RequestOpts;
}

export async function getServerRequestOpts(): Promise<RequestOpts> {
  const cookieStore = await cookies();
  const headerStore = await headers();
  const rawCookies = cookieStore
    .getAll()
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");
  const authorization = headerStore.get("authorization") ?? undefined;
  return { cookieHeader: rawCookies || undefined, authorization };
}

export const getServerPrincipal = cache(async (): Promise<ServerPrincipal | null> => {
  const requestOpts = await getServerRequestOpts();
  const session = await resolveSessionPrincipal(requestOpts);
  if (!session) return null;
  return {
    principalKind: session.principalKind,
    context: {
      principalId: session.principalId,
      tenantId: session.tenantId,
      contextId: `ctx:web:${session.principalId}`,
    },
    requestOpts,
  };
});
