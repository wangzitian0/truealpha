import { redirect } from "next/navigation";
import { ReadStateNotice } from "@/components/read-state";
import { loadTrace } from "@/server/dashboard";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function TracePage({
  searchParams,
}: {
  searchParams: Promise<{ issuer?: string; cutoff?: string }>;
}) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Ftrace");
  const params = await searchParams;
  const issuer = params.issuer ?? "";
  const cutoff = params.cutoff ?? "";
  const state = issuer && cutoff ? await loadTrace(principal.context, issuer, cutoff) : ({ kind: "empty" } as const);

  return (
    <section aria-labelledby="trace-heading" className="space-y-6">
      <div>
        <h1 id="trace-heading" className="text-2xl font-bold tracking-tight">
          Output trace
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Resolves a materialized output to its snapshot and normalized/raw references. Raw bytes are not returned by
          default.
        </p>
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <dl className="space-y-4 rounded-xl border border-border bg-card p-5 text-sm">
          <div>
            <dt className="text-gray-500">Trace ID</dt>
            <dd className="font-mono text-xs text-gray-200">{state.data.traceId}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Strategy / source</dt>
            <dd>
              {state.data.strategyId} — {state.data.source}
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Subject / cutoff</dt>
            <dd>
              {state.data.issuerId} @ {state.data.cutoffAt}
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Lineage</dt>
            <dd>
              <ul className="mt-2 space-y-2">
                {state.data.links.map((link) => (
                  <li key={link.kind} className="flex flex-wrap items-baseline gap-2">
                    <span className="rounded border border-border px-2 py-0.5 text-xs text-gray-400">{link.kind}</span>
                    <span className="text-gray-300">{link.label}:</span>
                    <span className="font-mono text-xs text-gray-400">{link.reference ?? "not exposed"}</span>
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}
