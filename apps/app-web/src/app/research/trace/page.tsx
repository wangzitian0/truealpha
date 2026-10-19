import { redirect } from "next/navigation";
import { formatIssuerLabel } from "@/client/format";
import { ReadStateNotice } from "@/components/read-state";
import { loadTrace } from "@/server/dashboard";
import { GovernedRunNotice, TraceInputsTable } from "@/components/trace-inputs-table";
import { getServerPrincipal } from "@/server/auth/request-context";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";

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
  const names = state.kind === "ready" ? await loadEntityDisplayMap() : new Map();

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

      {state.kind === "ready" && <GovernedRunNotice governed={state.data.governed} />}

      {state.kind === "ready" && (
        <dl className="space-y-4 rounded-xl border border-border bg-card p-5 text-sm">
          <div>
            <dt className="text-gray-500">Trace ID</dt>
            <dd data-evidence="true" className="font-mono text-xs text-gray-200">{state.data.traceId}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Strategy / source</dt>
            <dd>{`${state.data.strategyId} — ${state.data.source}`}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Subject / cutoff</dt>
            <dd title={state.data.issuerId} suppressHydrationWarning>
              <span>{formatIssuerLabel(state.data.issuerId, entityLabel(state.data.issuerId, names))}</span>{" "}
              <code
                data-evidence="true"
                className="ml-1 rounded bg-gray-500/15 px-1.5 py-0.5 font-mono text-xs text-gray-400"
              >
                {state.data.issuerId}
              </code>{" "}
              {`@ ${state.data.cutoffAt}`}
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Decision inputs</dt>
            <dd>
              <TraceInputsTable listings={state.data.listings} lineageResultId={state.data.lineageResultId} />
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Lineage</dt>
            <dd>
              <ul className="mt-2 space-y-2">
                {state.data.links.map((link) => (
                  <li key={link.kind} className="flex flex-wrap items-baseline gap-2">
                    <span className="rounded border border-border px-2 py-0.5 text-xs text-gray-400">{link.kind}</span>
                    <span className="text-gray-300">{`${link.label}:`}</span>
                    <span data-evidence="true" className="font-mono text-xs text-gray-400">{link.reference ?? "not exposed"}</span>
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
