/**
 * #493: THE administrator gate for the whole /admin/* prefix — one
 * server-side check here, no per-page re-implementation. Authorization is
 * evaluated per-request from the verified session (#371,
 * src/server/auth/request-context.ts's getServerPrincipal); this route
 * group must never be statically prerendered or cached, or a build-time
 * snapshot would leak into every later request.
 *
 * A logged-in non-administrator sees an explicit "access denied" message
 * instead of a silent bounce (deliberate, kept from the pre-#493 design);
 * anonymous visitors are redirected to /login like the research group.
 * Pages under here may assume an administrator principal but still derive
 * it themselves for their own reads (re-authorization per request).
 */

import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const principal = await getServerPrincipal();
  if (!principal) {
    redirect("/login?from=%2Fadmin");
  }
  if (principal.principalKind !== "administrator") {
    return (
      <section aria-labelledby="admin-denied-heading" className="space-y-4">
        <h1 id="admin-denied-heading" className="text-2xl font-bold tracking-tight">
          Operate
        </h1>
        <p role="status" className="rounded-lg border border-border bg-card p-4 text-amber-400">
          Access denied. This area requires a verified administrator identity.
        </p>
      </section>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-accent">Administrator</span>
        <span className="text-xs text-gray-500">— separately server-gated from research routes</span>
      </div>
      {children}
    </div>
  );
}
