import { PipelineHealth } from "./pipeline-health";
import { loadPipelineOverview } from "@/server/admin/pipeline";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function AdminPipelinePage() {
  const principal = await getServerPrincipal();
  const outcome = await loadPipelineOverview(
    principal === null ? null : { principalKind: principal.principalKind },
  );
  return <PipelineHealth outcome={outcome} now={Date.now()} />;
}
