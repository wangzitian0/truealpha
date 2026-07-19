import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadOverview } from "@/server/dashboard";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ResearchOverviewPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch");
  const state = await loadOverview(principal.context);

  return (
    <section aria-labelledby="overview-heading" className="space-y-8">
      <div>
        <h1 id="overview-heading" className="text-3xl font-bold tracking-tight">
          Dashboard
        </h1>
        <p className="mt-2 text-gray-400">
          Reads the governed head strategy run from the <code className="text-accent">mart</code> under the read-only
          role — no hardcoded list. Each module shows its materialized availability.
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
          {/* The run identity this page rendered — compare it one-to-one with the MCP
              strategy_run tool output (#370 appended acceptance criterion 3). */}
          <p className="text-sm text-gray-500">
            Strategy run{" "}
            <code className="text-accent">{state.data.run.strategyRunId ?? "(no run id recorded)"}</code>
            {state.data.run.executedAt ? ` executed ${state.data.run.executedAt}` : ""} — source{" "}
            <code>{state.data.run.source}</code>, corpus <code>{state.data.run.corpusSha256.slice(0, 12)}</code>.
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
