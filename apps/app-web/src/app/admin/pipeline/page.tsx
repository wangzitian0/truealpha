/**
 * #1466: the run health of every Dagster job. The view is `components/pipeline-health.tsx`.
 *
 * The /admin layout gates the administrator; the loader checks again (re-authorization per request).
 * The error text on this page is allowed because the whole prefix is administrator-only. It reaches
 * neither `mart.nightly_verdicts` nor /api/health.
 */

import { PipelineHealth } from "@/components/pipeline-health";
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
