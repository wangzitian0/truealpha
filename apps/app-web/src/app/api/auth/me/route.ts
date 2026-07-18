/** #368: GET /api/auth/me — session bootstrap for the client `AuthGuard`.
 * Derives AccessContext only from the verified cookie; never trusts a
 * client-supplied field. */

import { NextResponse, type NextRequest } from "next/server";
import { getRequestAccessContext } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const context = await getRequestAccessContext(request);
  if (!context) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  return NextResponse.json({
    principalId: context.principalId,
    tenantId: context.tenantId,
    issuedAt: context.issuedAt,
    expiresAt: context.expiresAt,
  });
}
