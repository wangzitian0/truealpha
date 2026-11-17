import { api, type RequestOpts } from "@/client/api";
import { SESSION_COOKIE_NAME } from "./config";

export interface SessionPrincipal {
  principalId: string;
  tenantId: string;
  principalKind: "member" | "administrator" | "service";
}

export async function resolveSessionPrincipal(opts?: RequestOpts): Promise<SessionPrincipal | null> {
  try {
    const res = await api.getSession(opts);
    if (!res.authenticated || !res.principal_id || !res.tenant_id || !res.principal_kind) return null;
    return {
      principalId: res.principal_id,
      tenantId: res.tenant_id,
      principalKind: res.principal_kind,
    };
  } catch {
    return null;
  }
}

export { SESSION_COOKIE_NAME };
