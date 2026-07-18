/**
 * #371: server-gated normal-user route group. Every page under here must
 * derive `AccessContext` only from `getServerPrincipal()` (the verified
 * session) — never a client-supplied field. This layout redirects to
 * /login before any child page renders if there is no verified session;
 * individual pages still re-derive the principal themselves for their own
 * mart reads (re-authorization per request, by design), not just for nav.
 */

import { redirect } from "next/navigation";
import { ResearchNav } from "@/components/research-nav";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ResearchLayout({ children }: { children: React.ReactNode }) {
  const principal = await getServerPrincipal();
  if (!principal) {
    redirect("/login?from=%2Fresearch");
  }

  return (
    <div className="space-y-8">
      <ResearchNav />
      {children}
    </div>
  );
}
