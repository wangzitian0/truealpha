import { api, ApiError, type PipelineOverviewResponse } from "@/client/api";
import type { ReadState } from "@/client/read-state";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PipelineHealth } from "./pipeline-health";

export const dynamic = "force-dynamic";

export default async function AdminPipelinePage() {
  const principal = await getServerPrincipal();
  let outcome: ReadState<PipelineOverviewResponse>;
  if (!principal || principal.principalKind !== "administrator") {
    outcome = { kind: "denied" };
  } else {
    try {
      const data = await api.getAdminPipeline(principal.requestOpts);
      outcome = data.available === false ? { kind: "unavailable", reason: data.reason ?? "unavailable" } : { kind: "ready", data };
    } catch (err) {
      outcome = err instanceof ApiError && (err.status === 401 || err.status === 403)
        ? { kind: "denied" }
        : { kind: "error", message: err instanceof Error ? err.message : String(err) };
    }
  }
  return <PipelineHealth outcome={outcome} />;
}
