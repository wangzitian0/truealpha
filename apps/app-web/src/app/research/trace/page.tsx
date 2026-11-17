import { api } from "@/client/api";
import { ReadStateNotice } from "@/components/read-state";
import { TraceInputsTable } from "@/components/trace-inputs-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function TracePage({ searchParams }: { searchParams: Promise<{ issuer?: string; cutoff?: string }> }) {
  const params = await searchParams;
  const issuer = params.issuer ?? "";
  const { data, error } = await loadAuthed("/research/trace", (opts) =>
    issuer ? api.getTrace({ issuer_id: issuer, cutoff: params.cutoff || undefined }, opts) : Promise.resolve(null),
  );

  return (
    <section aria-labelledby="trace-heading" className="space-y-6">
      <div>
        <h1 id="trace-heading" className="text-2xl font-bold tracking-tight">Output trace</h1>
        <p className="mt-2 text-sm text-gray-400">Resolves a materialized output to its <code className="text-accent">mart.metric_node_values</code> lineage.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {!issuer && <ReadStateNotice state={{ kind: "empty" }} />}
      {data && !data.available && <ReadStateNotice state={{ kind: "unavailable", reason: data.reason ?? "trace_not_found" }} />}
      {data?.available && (
        <dl className="space-y-4 rounded-xl border border-border bg-card p-5 text-sm">
          <div>
            <dt className="text-gray-500">Trace ID</dt>
            <dd data-evidence="true" className="font-mono text-xs text-gray-200">{data.trace_id || "—"}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Subject / cutoff</dt>
            <dd title={data.issuer_id}>
              <span>{data.label}</span>{" "}
              <code data-evidence="true" className="ml-1 rounded bg-gray-500/15 px-1.5 py-0.5 font-mono text-xs text-gray-400">{data.issuer_id}</code>
              {data.cutoff_at ? ` @ ${data.cutoff_at}` : ""}
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Decision inputs</dt>
            <dd>
              <TraceInputsTable
                nodeValues={data.node_values ?? []}
                lineageResultId={data.lineage_result_id ?? null}
                lineageInputs={data.lineage_inputs as never}
              />
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}
