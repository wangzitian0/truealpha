/**
 * #371: shell only. See conversations/page.tsx — same rationale. Document
 * persistence (owner-scoped reports/cards) is #373's contract, not yet
 * implemented; this route existing and being session-gated is what #371
 * claims.
 */

import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function LibraryPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");

  return (
    <section aria-labelledby="library-heading" className="space-y-4">
      <h1 id="library-heading" className="text-2xl font-bold tracking-tight">
        Library
      </h1>
      <p className="text-sm text-gray-400">
        Owner-scoped saved reports and cards are tracked separately (#373) and not yet
        implemented. This page confirms your session (
        <code className="text-accent">{principal.context.principalId}</code>) is verified and gated — no documents
        exist to show yet.
      </p>
    </section>
  );
}
