/**
 * #495 (surface 2): POST /admin/api/trigger — administrator-only manual
 * pipeline launch, mediated through Postgres (migration 0034 + the
 * data-engine sensor). Route handlers do NOT inherit the /admin layout
 * gate, so this handler re-derives and checks the principal itself; the
 * response echoes the request identity the admin page then shows linked to
 * the launched run.
 */

import { NextResponse } from "next/server";
import { requestPipelineTrigger } from "@/server/admin/trigger";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const principal = await getServerPrincipal();
  const body = await request.json().catch(() => ({}));
  const outcome = await requestPipelineTrigger(
    principal === null
      ? null
      : { principalId: principal.context.principalId, principalKind: principal.principalKind },
    typeof body?.executed_at === "string" ? body.executed_at : undefined,
  );

  switch (outcome.kind) {
    case "accepted":
      return NextResponse.json(
        {
          request_id: outcome.requestId,
          dedupe_key: outcome.dedupeKey,
          executed_at: outcome.executedAt,
          run_key: `manual:${outcome.dedupeKey}`,
        },
        { status: 202 },
      );
    case "denied":
      return NextResponse.json({ error: "administrator identity required" }, { status: 403 });
    case "invalid":
      return NextResponse.json({ error: outcome.message }, { status: 400 });
    case "error":
      return NextResponse.json({ error: outcome.message }, { status: 500 });
  }
}
