import Link from "next/link";
import { formatPrincipalLabel } from "@/client/format";
import { SignOutButton } from "@/components/sign-out-button";
import { getServerPrincipal } from "@/server/auth/request-context";

export async function AppChrome() {
  const principal = await getServerPrincipal();
  if (!principal) return null;
  const isAdministrator = principal.principalKind === "administrator";

  return (
    <div className="ml-auto flex items-center gap-3 text-sm">
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
        {formatPrincipalLabel(principal.context.principalId) ?? principal.context.principalId}
      </span>
      <SignOutButton />
    </div>
  );
}
