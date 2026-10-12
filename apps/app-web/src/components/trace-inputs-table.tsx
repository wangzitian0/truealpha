import { NOT_RECORDED, type TraceInput } from "@/server/mart/research-read";

/**
 * The inputs of one strategy decision (#1241): value, period, knowable_at, filing accession and
 * source vintage. Every cell is a field that `mart.topt_core_meta_info` carries. A field that the
 * lineage does not carry prints NOT_RECORDED. The component never invents or defaults a value.
 * It is a server component; it takes plain data and renders it.
 */
export function TraceInputsTable({
  inputs,
  lineageResultId,
}: {
  inputs: readonly TraceInput[];
  lineageResultId: string | null;
}) {
  return (
    <>
      <p className="mt-1 text-xs text-gray-500">
        Core result: {lineageResultId ?? NOT_RECORDED}. A field the lineage does not carry reads &quot;
        {NOT_RECORDED}&quot;.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-xs" data-testid="trace-inputs">
          <thead className="text-gray-500">
            <tr>
              <th className="pr-4 font-normal">Input</th>
              <th className="pr-4 font-normal">Value</th>
              <th className="pr-4 font-normal">Period</th>
              <th className="pr-4 font-normal">Knowable at</th>
              <th className="pr-4 font-normal">Filing accession</th>
              <th className="font-normal">Source vintage</th>
            </tr>
          </thead>
          <tbody className="font-mono text-gray-300">
            {inputs.map((input) => (
              <tr key={input.key} data-input={input.key} className="border-t border-border align-top">
                <td className="py-1 pr-4 font-sans text-gray-400">{input.label}</td>
                <td data-field="value" className="py-1 pr-4">
                  {input.value ?? NOT_RECORDED}
                </td>
                <td data-field="period" className="py-1 pr-4">
                  {input.period ?? NOT_RECORDED}
                </td>
                <td data-field="knowable_at" className="py-1 pr-4">
                  {input.knowableAt ?? NOT_RECORDED}
                </td>
                <td data-field="accession" className="py-1 pr-4">
                  {input.accession ?? NOT_RECORDED}
                </td>
                <td data-field="source_vintage" className="break-all py-1">
                  {input.sourceVintageId ?? NOT_RECORDED}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
