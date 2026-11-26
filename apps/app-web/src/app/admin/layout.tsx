import Link from "next/link";
import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fadmin");
  if (principal.principalKind !== "administrator") {
    return (
      <section aria-labelledby="admin-denied-heading" className="space-y-4">
        <h1 id="admin-denied-heading" className="text-2xl font-bold tracking-tight">Operate</h1>
        <p role="status" className="rounded-lg border border-border bg-card p-4 text-amber-400">
          Access denied. This area requires a verified administrator identity.
        </p>
      </section>
    );
  }
  return (
    <div className="space-y-6">
      <div className="-mx-4 border-t-[3px] border-amber-400" aria-hidden="true" />
      <div className="flex flex-wrap items-center gap-3" data-operate-chrome="true">
        <span className="text-xs font-semibold uppercase tracking-wider text-amber-400">Operate</span>
        <nav aria-label="Operate navigation" className="flex items-center gap-2 text-xs">
          <Link href="/admin" className="rounded-md border border-border bg-card px-2.5 py-1 text-gray-200 hover:border-amber-400 hover:text-white">
            Operations
          </Link>
          <Link href="/admin/datahub" className="rounded-md border border-border bg-card px-2.5 py-1 text-gray-200 hover:border-amber-400 hover:text-white">
            DataHub
          </Link>
        </nav>
        <span className="text-xs text-gray-500">— administrator world, separately server-gated from research</span>
        <Link href="/research" data-world-switch="research" className="ml-auto rounded-lg border border-border px-3 py-1 text-xs text-gray-300 hover:border-accent hover:text-white">
          ← Research
        </Link>
      </div>
      {children}
    </div>
  );
}
