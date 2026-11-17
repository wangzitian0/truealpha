import type { Schemas } from "@/client/api";
import { formatCell, formatNumberMagnitude, formatRatio } from "@/client/format";

export type TraceNodeValue = Schemas["TraceNodeValueDto"];

export interface TraceLineageInput {
  key: string;
  value?: string | null;
  period?: string | null;
  knowable_at?: string | null;
  accession?: string | null;
  source?: string | null;
}

const NR = "not recorded";

export function TraceInputsTable({
  nodeValues,
  lineageResultId = null,
  lineageInputs = [],
}: {
  nodeValues: readonly TraceNodeValue[];
  lineageResultId?: string | null;
  lineageInputs?: readonly TraceLineageInput[];
}) {
  const rows: TraceLineageInput[] =
    lineageInputs.length > 0
      ? [...lineageInputs]
      : nodeValues.map((n) => ({
          key: n.node_key,
          value: n.value ? `${formatNumberMagnitude(n.value) ?? n.value}${n.unit ? ` ${n.unit}` : ""}` : null,
          period: n.period ?? null,
          knowable_at: n.confidence ? formatRatio(n.confidence) : null,
          accession: n.availability_status,
          source: (n.reason_codes ?? []).join(", ") || null,
        }));
  return (
    <>
      <p className="mt-1 text-xs text-gray-500">{`Core result: ${lineageResultId ?? NR}.`}</p>
      <table className="mt-2 w-full text-left text-xs" data-testid="trace-inputs">
        <thead className="text-gray-500">
          <tr>
            <th className="pr-4 font-normal">Input</th>
            <th className="pr-4 font-normal">Value</th>
            <th className="pr-4 font-normal">Period</th>
            <th className="pr-4 font-normal">{lineageInputs.length > 0 ? "Knowable at" : "Confidence"}</th>
            <th className="pr-4 font-normal">{lineageInputs.length > 0 ? "Accession" : "Availability"}</th>
            <th className="font-normal">Source</th>
          </tr>
        </thead>
        <tbody className="font-mono text-gray-300">
          {rows.map((r) => (
            <tr key={`${r.key}-${r.period ?? ""}`} data-input={r.key} className="border-t border-border">
              <td className="py-1 pr-4 text-gray-200">{r.key}</td>
              <td data-field="value" className="py-1 pr-4 tabular-nums">{r.value ? (formatNumberMagnitude(r.value.split(" ")[0]) ? `${formatNumberMagnitude(r.value.split(" ")[0])}${r.value.includes(" ") ? ` ${r.value.split(" ").slice(1).join(" ")}` : ""}` : r.value) : NR}</td>
              <td data-field="period" className="py-1 pr-4">{r.period || NR}</td>
              <td data-field="knowable_at" className="py-1 pr-4">{r.knowable_at || NR}</td>
              <td data-field="accession" className="py-1 pr-4">{r.accession || NR}</td>
              <td data-field="source" className="py-1 text-gray-400">{r.source || NR}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
