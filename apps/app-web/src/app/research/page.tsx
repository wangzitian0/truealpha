import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadOverview } from "@/server/dashboard";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ResearchOverviewPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch");
  const state = loadOverview(principal.context);

  return (
    <section aria-labelledby="overview-heading" className="space-y-8">
      <div>
        <h1 id="overview-heading" className="text-3xl font-bold tracking-tight">
          Dashboard
        </h1>
        <p className="mt-2 text-gray-400">
          Reads through the <code className="text-accent">mart</code> read adapter — no hardcoded list. Fixture-backed
          pending #41&apos;s mart-backed read role; only the adapter changes when it lands. Each module shows its
          materialized availability.
        </p>
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          <p className="text-sm text-gray-500">
            {state.data.latestCutoff
              ? `Latest materialized cutoff: ${state.data.latestCutoff}.`
              : "No materialized cutoff yet."}
          </p>
          <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {state.data.modules.map((module) => (
              <li key={module.module} className="rounded-xl border border-border bg-card p-5">
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-500">Module {module.module}</span>
                  <span className="rounded-full border border-border px-2 py-0.5 text-xs text-gray-400">
                    {module.gate}
                  </span>
                </div>
                <h2 className="mt-2 font-semibold">{module.name}</h2>
                <p className="mt-1 text-sm text-gray-400">{module.note}</p>
                <div className="mt-3">
                  <AvailabilityBadge status={module.availability} />
                </div>
              </li>
            ))}
          </ul>
          <p className="text-sm text-gray-500">
            Explore the{" "}
            <Link href="/research/rankings" className="text-accent hover:underline">
              theme rankings
            </Link>{" "}
            or{" "}
            <Link href="/research/compare" className="text-accent hover:underline">
              issuer comparison
            </Link>
            .
          </p>
        </>
      )}
    </section>
  );
}
