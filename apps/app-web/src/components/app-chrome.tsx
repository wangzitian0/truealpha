/**
 * #371: the top bar's identity and world switch.
 *
 * #371's own acceptance said "logged-in member sees member nav; administrator
 * sees admin nav" — and closed at the exact second its PR merged, via
 * `Closes #371`, with nobody walking that criterion. There was no admin nav,
 * no link to `/admin` from anywhere in the application, no link back, and no
 * indication of who was signed in. `research-nav.tsx` even carried a comment
 * asserting the "separate navs" that were never built. The two route groups
 * really are separately server-gated (tests/route-group-boundary.test.ts proves
 * the import graph); what was missing was any way to move between them.
 *
 * Deliberately renders NOTHING for an anonymous request rather than a "sign in"
 * affordance: `/login` is reached by redirect from the gates, and an empty bar
 * there is correct.
 *
 * This component reads identity only. It never imports an administrator server
 * module — the boundary scan covers `src/components`, and the world switch is
 * driven by `principalKind` from the verified session, never by a client field.
 */

import Link from "next/link";
import { SignOutButton } from "@/components/sign-out-button";
import { getServerPrincipal } from "@/server/auth/request-context";

export async function AppChrome() {
  const principal = await getServerPrincipal();
  if (!principal) return null;

  const isAdministrator = principal.principalKind === "administrator";

  return (
    <div className="ml-auto flex items-center gap-3 text-sm">
      {/* The world switch. Present for an administrator on every page, absent
          for a member — asserted per role by e2e/walk-tree.mjs, not by a
          comment this time. */}
      {isAdministrator && (
        <Link
          href="/admin"
          data-world-switch="admin"
          className="rounded-lg border border-amber-400/40 px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-amber-400 hover:border-amber-400 hover:text-amber-300"
        >
          Operate
        </Link>
      )}
      <span
        data-signed-in-as="true"
        className="max-w-[16rem] truncate text-gray-400"
        title={`${principal.context.principalId} · ${principal.context.tenantId}`}
      >
        {principal.context.principalId}
      </span>
      {/* #540: the session's other end. The endpoint has worked since #368;
          nothing ever called it. */}
      <SignOutButton />
    </div>
  );
}
