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

import Link from "next/link";
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

  // #494: the OPERATE chrome — a 3px amber bar + eyebrow on EVERY /admin
  // page, from this one layout. Any screenshot or shared tab is instantly
  // recognizable as the operator world (and never mistakable for research).
  return (
    <div className="space-y-6">
      <div className="-mx-4 border-t-[3px] border-amber-400" aria-hidden="true" />
      <div className="flex items-center gap-2" data-operate-chrome="true">
        <span className="text-xs font-semibold uppercase tracking-wider text-amber-400">Operate</span>
        <span className="text-xs text-gray-500">— administrator world, separately server-gated from research</span>
        {/* #371: the return leg. The Operate world had no way out — an
            administrator who typed /admin had to type /research to get back.
            One hop, from this one layout, so every page under the prefix has
            it; e2e/walk-tree.mjs asserts it on each of them. */}
        <Link
          href="/research"
          data-world-switch="research"
          className="ml-auto rounded-lg border border-border px-3 py-1 text-xs text-gray-300 hover:border-accent hover:text-white"
        >
          ← Research
        </Link>
      </div>
      {children}
    </div>
  );
}
