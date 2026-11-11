import Link from "next/link";
import { formatCell, formatNumberMagnitude, formatRatio, formatUsdMagnitude } from "@/client/format";
import { exclusionLabel } from "@/contracts/exclusion-reasons";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { DASHBOARD_STRATEGY_ID, WIDE_PANEL_INPUT_COLUMNS } from "@/server/mart/research-read";
import { loadStrategyRunPage } from "@/server/strategy-page";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function CoveragePage() {
  const principal = await getServerPrincipal();
  const outcome = await loadStrategyRunPage(principal, DASHBOARD_STRATEGY_ID);

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="coverage-heading">
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">My data coverage</h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-gray-400">
          {outcome.kind === "unavailable"
            ? `No strategy run recorded yet (${outcome.detail.reason}).`
            : outcome.kind === "empty"
              ? "No decisions recorded for this strategy run."
              : outcome.kind === "error"
                ? `Error loading coverage: ${outcome.message}`
                : "Access denied. No verified session for this request."}
        </p>
      </section>
    );
  }

  const names = await loadEntityDisplayMap();
  const decisions = [...outcome.report.decisions].sort((a, b) =>
    entityLabel(a.issuer_id, names).localeCompare(entityLabel(b.issuer_id, names)),
  );
  const covered = decisions.filter((d) => d.exclusion_reason === null).length;
  const widePanel = outcome.widePanel ?? {};

  return (
    <section aria-labelledby="coverage-heading" className="space-y-6">
      <div>
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">My data coverage</h1>
        <p className="mt-2 text-sm text-gray-400">
          {covered} of {decisions.length} companies have every input the strategy needs; the rest
          say exactly what is missing. Deep per-layer diagnostics live in the operator world.
        </p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Per-company input coverage for the latest strategy run</caption>
          <thead className="bg-card text-xs uppercase text-gray-500">
            <tr>
              {["Company", "Inputs", "What is missing"].map((h) => (
                <th key={h} scope="col" className="px-4 py-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {decisions.map((d) => (
              <tr key={d.issuer_id} className="border-t border-border">
                <th scope="row" className="px-4 py-3 font-medium">
                  <Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>
                    {entityLabel(d.issuer_id, names)}
                  </Link>
                </th>
                <td className="px-4 py-3">
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs ${
                      d.exclusion_reason === null ? "bg-emerald-400/10 text-emerald-400" : "bg-red-400/10 text-red-400"
                    }`}
                  >
                    {d.exclusion_reason === null ? "complete" : "incomplete"}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-400">{formatCell(exclusionLabel(d.exclusion_reason))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3" data-testid="coverage-wide-panel-section">
        <div>
          <h2 className="text-lg font-semibold text-gray-200">
            Standardized Wide Input Panel (Company × Metric + Confidence)
          </h2>
          <p className="mt-1 text-xs text-gray-400">
            Period-aligned, unit-normalized wide-panel cells and consumed cell confidence (
            <code className="font-mono text-gray-300">conf__&lt;metric&gt;</code>) from{" "}
            <code className="font-mono text-gray-300">mart.metric_node_values</code> alongside strategy decision outputs.
          </p>
        </div>

        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-left text-sm" data-testid="coverage-wide-panel-table">
            <caption className="sr-only">Company by standardized wide-panel field and confidence</caption>
            <thead className="bg-card text-xs uppercase text-gray-500">
              <tr>
                {["Company", "Run Confidence", "Labor Efficiency", "P/S", "P/GP", "PEG"].map((h) => (
                  <th key={h} scope="col" className="px-4 py-3">{h}</th>
                ))}
                {WIDE_PANEL_INPUT_COLUMNS.map((col) => (
                  <th key={col.key} scope="col" className="px-4 py-3 font-mono">{col.key}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => {
                const issuerCells = widePanel[d.issuer_id] ?? {};
                return (
                  <tr key={d.issuer_id} className="border-t border-border">
                    <th scope="row" className="px-4 py-3 font-medium">
                      <Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>
                        {entityLabel(d.issuer_id, names)}
                      </Link>
                    </th>
                    <td className="px-4 py-3 font-mono tabular-nums" title={d.confidence ?? undefined}>
                      {formatCell(formatRatio(d.confidence))}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={d.capital_adjusted_labor_efficiency ?? undefined}>
                      <div>{formatCell(formatUsdMagnitude(d.capital_adjusted_labor_efficiency))}</div>
                      {d.labor_efficiency_metric && <div className="text-[11px] text-gray-500">{d.labor_efficiency_metric}</div>}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={d.current_price_to_sales ?? undefined}>
                      {formatCell(formatRatio(d.current_price_to_sales))}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={d.current_price_to_gross_profit ?? undefined}>
                      {formatCell(formatRatio(d.current_price_to_gross_profit ?? null))}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={d.peg ?? undefined}>
                      {formatCell(formatRatio(d.peg ?? null))}
                    </td>
                    {WIDE_PANEL_INPUT_COLUMNS.map((col) => {
                      const node = issuerCells[col.key];
                      const formattedVal = node?.value
                        ? col.monetary
                          ? formatUsdMagnitude(node.value)
                          : formatNumberMagnitude(node.value)
                        : null;
                      return (
                        <td key={col.key} className="px-4 py-3 font-mono tabular-nums" title={node?.value ?? undefined}>
                          <div>{formatCell(formattedVal)}</div>
                          {node?.confidence && (
                            <div className="text-[11px] text-gray-500" title={node.confidence}>
                              conf {formatCell(formatRatio(node.confidence))}
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
