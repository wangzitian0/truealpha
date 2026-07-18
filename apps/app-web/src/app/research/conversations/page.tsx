/**
 * #371: shell only. Derives AccessContext from the verified session (the
 * layout already redirects an unauthenticated visitor, this re-derives per
 * #368's re-authorize-every-request rule) but does not read or persist any
 * conversation yet — that contract is #396's scope. This route existing and
 * being session-gated is what #371 claims; conversation content is not.
 */

import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ConversationsPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fconversations");

  return (
    <section aria-labelledby="conversations-heading" className="space-y-4">
      <h1 id="conversations-heading" className="text-2xl font-bold tracking-tight">
        Conversations
      </h1>
      <p className="text-sm text-gray-400">
        Conversation persistence is tracked separately (#396) and not yet implemented. This
        page confirms your session (<code className="text-accent">{principal.context.principalId}</code>) is verified
        and gated — no conversation content exists to show yet.
      </p>
    </section>
  );
}
