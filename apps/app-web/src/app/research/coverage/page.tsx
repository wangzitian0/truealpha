/**
 * #494: the researcher's data-coverage view — per company, "what backs my
 * conclusions and what's missing", in plain language. Rows come from the
 * SAME governed strategy read the strategy page uses (mart, latest run);
 * "what's missing" is the single exclusion-reason translation table
 * (src/contracts/exclusion-reasons.ts), so operators fix a gap once and
 * both worlds' wording moves together. The deep per-layer diagnostic
 * stays in /admin/quality — this page answers the researcher's question
 * only.
 */

import Link from "next/link";
import {
  formatCell,
  formatNumberMagnitude,
  formatRatio,
  formatUsdMagnitude,
} from "@/client/format";
import { exclusionLabel } from "@/contracts/exclusion-reasons";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import {
  DASHBOARD_STRATEGY_ID,
  WIDE_PANEL_INPUT_COLUMNS,
} from "@/server/mart/research-read";
import { loadStrategyRunPage } from "@/server/strategy-page";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function CoveragePage() {
  const principal = await getServerPrincipal();
  const outcome = await loadStrategyRunPage(principal, DASHBOARD_STRATEGY_ID);

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="coverage-heading">
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">
          My data coverage
        </h1>
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
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">
          My data coverage
        </h1>
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
              <th scope="col" className="px-4 py-3">Company</th>
              <th scope="col" className="px-4 py-3">Inputs</th>
              <th scope="col" className="px-4 py-3">What is missing</th>
            </tr>
          </thead>
          <tbody>
            {decisions.map((decision) => (
              <tr key={decision.issuer_id} className="border-t border-border">
                <th scope="row" className="px-4 py-3 font-medium">
                  <Link
                    href={`/research/entities/${encodeURIComponent(decision.issuer_id)}`}
                    className="text-accent hover:underline"
                    title={decision.issuer_id}
                  >
                    {entityLabel(decision.issuer_id, names)}
                  </Link>
                </th>
                <td className="px-4 py-3">
                  {decision.exclusion_reason === null ? (
                    <span className="rounded-full bg-emerald-400/10 px-2.5 py-0.5 text-xs text-emerald-400">
                      complete
                    </span>
                  ) : (
                    <span className="rounded-full bg-red-400/10 px-2.5 py-0.5 text-xs text-red-400">
                      incomplete
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-400">{formatCell(exclusionLabel(decision.exclusion_reason))}</td>
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
                <th scope="col" className="px-4 py-3">Company</th>
                <th scope="col" className="px-4 py-3">Run Confidence</th>
                <th scope="col" className="px-4 py-3">Labor Efficiency</th>
                <th scope="col" className="px-4 py-3">P/S</th>
                <th scope="col" className="px-4 py-3">P/GP</th>
                <th scope="col" className="px-4 py-3">PEG</th>
                {WIDE_PANEL_INPUT_COLUMNS.map((col) => (
                  <th key={col.key} scope="col" className="px-4 py-3 font-mono">
                    {col.key}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {decisions.map((decision) => {
                const issuerCells = widePanel[decision.issuer_id] ?? {};
                return (
                  <tr key={decision.issuer_id} className="border-t border-border">
                    <th scope="row" className="px-4 py-3 font-medium">
                      <Link
                        href={`/research/entities/${encodeURIComponent(decision.issuer_id)}`}
                        className="text-accent hover:underline"
                        title={decision.issuer_id}
                      >
                        {entityLabel(decision.issuer_id, names)}
                      </Link>
                    </th>
                    <td className="px-4 py-3 font-mono tabular-nums" title={decision.confidence ?? undefined}>
                      {formatCell(formatRatio(decision.confidence))}
                    </td>
                    <td
                      className="px-4 py-3 font-mono tabular-nums"
                      title={decision.capital_adjusted_labor_efficiency ?? undefined}
                    >
                      <div>{formatCell(formatUsdMagnitude(decision.capital_adjusted_labor_efficiency))}</div>
                      {decision.labor_efficiency_metric && (
                        <div className="text-[11px] text-gray-500">{decision.labor_efficiency_metric}</div>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={decision.current_price_to_sales ?? undefined}>
                      {formatCell(formatRatio(decision.current_price_to_sales))}
                    </td>
                    <td
                      className="px-4 py-3 font-mono tabular-nums"
                      title={decision.current_price_to_gross_profit ?? undefined}
                    >
                      {formatCell(formatRatio(decision.current_price_to_gross_profit ?? null))}
                    </td>
                    <td className="px-4 py-3 font-mono tabular-nums" title={decision.peg ?? undefined}>
                      {formatCell(formatRatio(decision.peg ?? null))}
                    </td>
                    {WIDE_PANEL_INPUT_COLUMNS.map((col) => {
                      const nodeCell = issuerCells[col.key];
                      const formattedVal = nodeCell?.value
                        ? col.monetary
                          ? formatUsdMagnitude(nodeCell.value)
                          : formatNumberMagnitude(nodeCell.value)
                        : null;
                      return (
                        <td key={col.key} className="px-4 py-3 font-mono tabular-nums" title={nodeCell?.value ?? undefined}>
                          <div>{formatCell(formattedVal)}</div>
                          {nodeCell?.confidence && (
                            <div className="text-[11px] text-gray-500" title={nodeCell.confidence}>
                              conf {formatCell(formatRatio(nodeCell.confidence))}
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
