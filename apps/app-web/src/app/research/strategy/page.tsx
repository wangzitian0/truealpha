import { loadStrategyRunPage } from "@/server/strategy-page";
import { entityLabel, loadEntityDisplayMap, type EntityDisplay } from "@/server/mart/entity-resolution";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { formatPercentFromFraction, formatRatio, formatSignedRatio, signColor } from "@/client/format";
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

/** The non-`ready` states, rendered as one notice line. Kept beside the
 * claim ceiling rather than in an early return: see the banner placement
 * note on the component below. */
const NOTICE_STYLE = {
  denied: "text-amber-400",
  error: "text-red-400",
  unavailable: "text-gray-400",
} as const;

export default async function StrategyRunsPage() {
  const principal = await getServerPrincipal();
  const outcome = await loadStrategyRunPage(principal, STRATEGY_ID);
  // Authorization first: a denied request must not reach a mart read (the
  // guard pattern `dashboard.ts` documents), so this stays conditional.
  const names: Map<string, EntityDisplay> =
    outcome.kind === "ready" ? await loadEntityDisplayMap() : new Map();

  return (
    <section aria-labelledby="strategy-runs-heading">
      <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">
        Strategy runs
      </h1>

      {outcome.kind === "ready" ? (
        <p className="mt-2 text-sm text-gray-400">
          {outcome.report.strategy_id} — provisional source: {outcome.report.source}. Corpus{" "}
          <code className="text-accent">{outcome.report.corpus_sha256}</code>.
          {outcome.report.golden_mismatches.length > 0 && (
            <span role="status" className="ml-2 text-red-400">
              {outcome.report.golden_mismatches.length} golden mismatch(es).
            </span>
          )}
        </p>
      ) : (
        <p
          role="status"
          className={`mt-4 rounded-lg border border-border bg-card p-4 ${NOTICE_STYLE[outcome.kind]}`}
        >
          {outcome.kind === "denied"
            ? "Access denied. No verified session for this request."
            : outcome.kind === "error"
              ? `Error loading strategy run: ${outcome.message}`
              : `Unavailable: ${outcome.detail.reason} (${outcome.detail.strategy_id})`}
        </p>
      )}

      {/* #494 assertion (b): the claim ceiling is a property of the PAGE, not
          of its data state — `/research/rankings` has always rendered it
          unconditionally, and `claim-ceiling.tsx` says so in its own docstring.
          It used to sit after three early returns here, so a cutoff with no
          recorded run dropped it. That divergence is invisible on any database
          holding a strategy run and fails on every one that does not, which is
          what a fresh CI database is. tests/claim-ceiling-placement.test.ts
          fails if an early return is reintroduced above this line. */}
      <ClaimCeilingBanner />

      {outcome.kind === "ready" && (
        <div className="mt-6 overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Decisions for {outcome.report.strategy_id}, by issuer and cutoff</caption>
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
              {outcome.report.decisions.map((decision) => (
                <tr key={`${decision.issuer_id}:${decision.cutoff_at}`} className="border-t border-border">
                  <th scope="row" className="px-4 py-3 font-medium" title={decision.issuer_id}>
                    {entityLabel(decision.issuer_id, names)}
                  </th>
                  <td className="px-4 py-3">{decision.cutoff_at}</td>
                  <td className="px-4 py-3">{decisionLabel(decision)}</td>
                  <td className="px-4 py-3">{cell(decision.tier)}</td>
                  <td
                    className={`px-4 py-3 tabular-nums ${signColor(decision.valuation_gap)}`}
                    title={decision.valuation_gap ?? undefined}
                  >
                    {cell(formatSignedRatio(decision.valuation_gap))}
                  </td>
                  <td className="px-4 py-3 tabular-nums" title={decision.confidence ?? undefined}>
                    {cell(formatRatio(decision.confidence))}
                  </td>
                  <td className="px-4 py-3 tabular-nums">{decision.rank ?? "—"}</td>
                  <td className="px-4 py-3 tabular-nums" title={decision.target_weight ?? undefined}>
                    {cell(formatPercentFromFraction(decision.target_weight))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
