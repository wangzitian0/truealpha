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

import { exclusionLabel } from "@/contracts/exclusion-reasons";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { loadStrategyRunPage } from "@/server/strategy-page";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

const STRATEGY_ID = "large_model_value_v0";

export default async function CoveragePage() {
  const principal = await getServerPrincipal();
  const outcome = await loadStrategyRunPage(principal, STRATEGY_ID);

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="coverage-heading">
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">
          My data coverage
        </h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-gray-400">
          {outcome.kind === "unavailable"
            ? `No strategy run recorded yet (${outcome.detail.reason}).`
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
                <th scope="row" className="px-4 py-3 font-medium" title={decision.issuer_id}>
                  {entityLabel(decision.issuer_id, names)}
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
                <td className="px-4 py-3 text-gray-400">{exclusionLabel(decision.exclusion_reason) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
