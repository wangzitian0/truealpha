import {
  NOT_RECORDED,
  type KnowableAtBasis,
  type TraceInput,
  type TraceListing,
} from "@/server/mart/research-read";

/**
 * The inputs of one strategy decision (#1241): value with its currency, period, the filing date or
 * knowable_at, filing accession and source. Every cell is a field that `mart.topt_core_meta_info`
 * carries. A field that the lineage does not carry prints NOT_RECORDED. The component never invents
 * or defaults a value. It is a server component; it takes plain data and renders it.
 */

const BASIS_TEXT: Record<KnowableAtBasis, string> = {
  filed: "filed",
  input: "input knowable_at",
  observation: "observation knowable_at",
};

function valueText(input: TraceInput): string {
  if (input.value === null) return NOT_RECORDED;
  if (!input.monetary) return input.value;
  return `${input.value} ${input.currency ?? `(currency ${NOT_RECORDED})`}`;
}

function knowableAtText(input: TraceInput): string {
  if (input.knowableAt === null || input.knowableAtBasis === null) return NOT_RECORDED;
  return `${input.knowableAt} (${BASIS_TEXT[input.knowableAtBasis]})`;
}

function sourceText(input: TraceInput): string {
  if (input.source === null || input.sourceKind === null) return NOT_RECORDED;
  return `${input.source} (${input.sourceKind})`;
}

/** The run is the governed head, or the page says it is not (#1241 audit). */
export function GovernedRunNotice({ governed }: { governed: boolean | null }) {
  if (governed === true) return null;
  const text =
    governed === false
      ? "This run is not the governed head. No head resolves a run for this strategy, so the page shows the newest recorded run."
      : `Whether this run is the governed head is ${NOT_RECORDED}.`;
  return (
    <p data-testid="trace-run-notice" className="rounded border border-amber-500/40 px-3 py-2 text-xs text-amber-300">
      {text}
    </p>
  );
}

export function TraceInputsTable({
  listings,
  lineageResultId,
}: {
  listings: readonly TraceListing[];
  lineageResultId: string | null;
}) {
  return (
    <>
      <p className="mt-1 text-xs text-gray-500">
        Core result: {lineageResultId ?? NOT_RECORDED}. A field the lineage does not carry reads &quot;
        {NOT_RECORDED}&quot;. A listing is one traded class of the issuer.
      </p>
      {listings.map((listing) => (
        <div
          key={listing.listingId ?? NOT_RECORDED}
          data-listing={listing.listingId ?? NOT_RECORDED}
          className="mt-3 overflow-x-auto"
        >
          <p className="font-mono text-xs text-gray-300">
            Listing: {listing.listingId ?? NOT_RECORDED}
            {listing.isResultListing ? " (listing named by the core result)" : ""}
          </p>
          <table className="mt-1 w-full text-left text-xs" data-testid="trace-inputs">
            <thead className="text-gray-500">
              <tr>
                <th className="pr-4 font-normal">Input</th>
                <th className="pr-4 font-normal">Value</th>
                <th className="pr-4 font-normal">Period</th>
                <th className="pr-4 font-normal">Filed / knowable at</th>
                <th className="pr-4 font-normal">Filing accession</th>
                <th className="font-normal">Source</th>
              </tr>
            </thead>
            <tbody className="font-mono text-gray-300">
              {listing.inputs.map((input) => (
                <tr key={input.key} data-input={input.key} className="border-t border-border align-top">
                  <td className="py-1 pr-4 font-sans text-gray-400">{input.label}</td>
                  <td data-field="value" className="py-1 pr-4">
                    {valueText(input)}
                  </td>
                  <td data-field="period" className="py-1 pr-4">
                    {input.period ?? NOT_RECORDED}
                  </td>
                  <td data-field="knowable_at" className="py-1 pr-4">
                    {knowableAtText(input)}
                  </td>
                  <td data-field="accession" className="py-1 pr-4">
                    {input.accession ?? NOT_RECORDED}
                  </td>
                  <td data-field="source" className="break-all py-1">
                    {sourceText(input)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
