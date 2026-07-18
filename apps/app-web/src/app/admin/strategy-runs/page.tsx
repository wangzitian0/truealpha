import { loadStrategyRunPage } from "@/server/admin-strategy-runs";
import { getServerPrincipal } from "@/server/auth/request-context";
import type { StrategyRunDecision, StrategyRunOutcome } from "@/contracts/strategyRun";

const STRATEGY_ID = "large_model_value_v0";

const OUTCOME_LABEL: Record<StrategyRunOutcome, string> = {
  selected: "Selected",
  ranked_beyond_selection_count: "Ranked (not selected)",
  rejected_valuation_above_tier_band: "Rejected — valuation above tier band",
  excluded: "Excluded",
};

function decisionLabel(decision: StrategyRunDecision): string {
  const base = OUTCOME_LABEL[decision.outcome];
  return decision.exclusion_reason ? `${base}: ${decision.exclusion_reason}` : base;
}

function cell(value: string | null): string {
  return value ?? "—";
}

export default async function StrategyRunsPage() {
  const principal = await getServerPrincipal();
  const outcome = loadStrategyRunPage(principal, STRATEGY_ID);

  if (outcome.kind === "denied") {
    return (
      <section aria-labelledby="strategy-runs-heading">
        <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">
          Strategy runs
        </h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-amber-400">
          Access denied. No verified administrator identity for this request.
        </p>
      </section>
    );
  }

  if (outcome.kind === "error") {
    return (
      <section aria-labelledby="strategy-runs-heading">
        <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">
          Strategy runs
        </h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-red-400">
          Error loading strategy run: {outcome.message}
        </p>
      </section>
    );
  }

  if (outcome.kind === "unavailable") {
    return (
      <section aria-labelledby="strategy-runs-heading">
        <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">
          Strategy runs
        </h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-gray-400">
          Unavailable: {outcome.detail.reason} ({outcome.detail.strategy_id})
        </p>
      </section>
    );
  }

  const { report } = outcome;

  return (
    <section aria-labelledby="strategy-runs-heading">
      <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">
        Strategy runs
      </h1>
      <p className="mt-2 text-sm text-gray-400">
        {report.strategy_id} — provisional source: {report.source}. Corpus{" "}
        <code className="text-accent">{report.corpus_sha256}</code>.
        {report.golden_mismatches.length > 0 && (
          <span role="status" className="ml-2 text-red-400">
            {report.golden_mismatches.length} golden mismatch(es).
          </span>
        )}
      </p>

      <div className="mt-6 overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Decisions for {report.strategy_id}, by issuer and cutoff</caption>
          <thead className="bg-card text-xs uppercase text-gray-500">
            <tr>
              <th scope="col" className="px-4 py-3">
                Issuer
              </th>
              <th scope="col" className="px-4 py-3">
                Cutoff
              </th>
              <th scope="col" className="px-4 py-3">
                Status
              </th>
              <th scope="col" className="px-4 py-3">
                Tier
              </th>
              <th scope="col" className="px-4 py-3">
                Valuation gap
              </th>
              <th scope="col" className="px-4 py-3">
                Confidence
              </th>
              <th scope="col" className="px-4 py-3">
                Rank
              </th>
              <th scope="col" className="px-4 py-3">
                Weight
              </th>
            </tr>
          </thead>
          <tbody>
            {report.decisions.map((decision) => (
              <tr key={`${decision.issuer_id}:${decision.cutoff_at}`} className="border-t border-border">
                <th scope="row" className="px-4 py-3 font-medium">
                  {decision.issuer_id}
                </th>
                <td className="px-4 py-3">{decision.cutoff_at}</td>
                <td className="px-4 py-3">{decisionLabel(decision)}</td>
                <td className="px-4 py-3">{cell(decision.tier)}</td>
                <td className="px-4 py-3">{cell(decision.valuation_gap)}</td>
                <td className="px-4 py-3">{cell(decision.confidence)}</td>
                <td className="px-4 py-3">{decision.rank ?? "—"}</td>
                <td className="px-4 py-3">{cell(decision.target_weight)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
