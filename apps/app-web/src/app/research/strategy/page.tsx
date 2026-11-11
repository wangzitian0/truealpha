import Link from "next/link";
import { loadStrategyRunPage } from "@/server/strategy-page";
import { entityLabel, loadEntityDisplayMap, type EntityDisplay } from "@/server/mart/entity-resolution";
import { DASHBOARD_STRATEGY_ID } from "@/server/mart/research-read";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { ResearchTable } from "@/components/research-table";
import { StrategyDefinitionLine } from "@/components/strategy-definition";
import { formatCell, formatPercentFromFraction, formatRatio, formatSignedRatio, signColor } from "@/client/format";
import { getServerPrincipal } from "@/server/auth/request-context";
import {
  currentMultipleOf,
  reportValuationBasis,
  targetMultipleOf,
  valuationBasisLabel,
  type StrategyRunDecision,
  type StrategyRunOutcome,
} from "@/contracts/strategyRun";

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

const NOTICE_STYLE = {
  denied: "text-amber-400",
  empty: "text-gray-400",
  error: "text-red-400",
  unavailable: "text-gray-400",
} as const;

export default async function StrategyRunsPage() {
  const principal = await getServerPrincipal();
  const outcome = await loadStrategyRunPage(principal, DASHBOARD_STRATEGY_ID);
  const names: Map<string, EntityDisplay> = outcome.kind === "ready" ? await loadEntityDisplayMap() : new Map();
  const basisLabel = valuationBasisLabel(
    outcome.kind === "ready" ? reportValuationBasis(outcome.report.decisions) : null,
  );
  const headers = [
    "Issuer",
    "Cutoff",
    "Status",
    "Tier",
    `Current ${basisLabel}`,
    `Target ${basisLabel}`,
    `Valuation gap (${basisLabel})`,
    "Confidence",
    "Rank",
    "Weight",
  ];

  return (
    <section aria-labelledby="strategy-runs-heading">
      <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">Strategy runs</h1>

      {outcome.kind === "ready" ? (
        <p className="mt-2 text-sm text-gray-400">
          {outcome.report.strategy_id} — provisional source: {outcome.report.source}. Corpus{" "}
          <code className="text-accent">{outcome.report.corpus_sha256}</code>.
          {outcome.report.golden_mismatches.length > 0 && (
            <>
              {" "}
              <span role="status" className="ml-2 text-red-400">
                {outcome.report.golden_mismatches.length} golden mismatch(es).
              </span>
            </>
          )}
        </p>
      ) : (
        <p role="status" className={`mt-4 rounded-lg border border-border bg-card p-4 ${NOTICE_STYLE[outcome.kind]}`}>
          {outcome.kind === "denied"
            ? "Access denied. No verified session for this request."
            : outcome.kind === "empty"
              ? "No decisions recorded for this strategy run."
              : outcome.kind === "error"
                ? `Error loading strategy run: ${outcome.message}`
                : `Unavailable: ${outcome.detail.reason} (${outcome.detail.strategy_id})`}
        </p>
      )}
      {outcome.kind === "ready" && <StrategyDefinitionLine definition={outcome.report.definition} />}

      <ClaimCeilingBanner />

      {outcome.kind === "ready" && (
        <ResearchTable
          className="mt-6 overflow-x-auto rounded-xl border border-border"
          caption={`Decisions for ${outcome.report.strategy_id}, by issuer and cutoff`}
          headers={headers}
        >
          {outcome.report.decisions.map((d) => (
            <tr key={`${d.issuer_id}:${d.cutoff_at}`} className="border-t border-border">
              <th scope="row" className="px-4 py-3 font-medium">
                <Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>
                  {entityLabel(d.issuer_id, names)}
                </Link>
              </th>
              <td className="px-4 py-3">{d.cutoff_at}</td>
              <td className="px-4 py-3">{decisionLabel(d)}</td>
              <td className="px-4 py-3">{formatCell(d.tier)}</td>
              <td className="px-4 py-3 tabular-nums" title={currentMultipleOf(d) ?? undefined}>
                {formatCell(formatRatio(currentMultipleOf(d)))}
              </td>
              <td className="px-4 py-3 tabular-nums" title={targetMultipleOf(d) ?? undefined}>
                {formatCell(formatRatio(targetMultipleOf(d)))}
              </td>
              <td className={`px-4 py-3 tabular-nums ${signColor(d.valuation_gap)}`} title={d.valuation_gap ?? undefined}>
                {formatCell(formatSignedRatio(d.valuation_gap))}
              </td>
              <td className="px-4 py-3 tabular-nums" title={d.confidence ?? undefined}>
                {formatCell(formatRatio(d.confidence))}
              </td>
              <td className="px-4 py-3 tabular-nums">{formatCell(d.rank)}</td>
              <td className="px-4 py-3 tabular-nums" title={d.target_weight ?? undefined}>
                {formatCell(formatPercentFromFraction(d.target_weight))}
              </td>
            </tr>
          ))}
        </ResearchTable>
      )}
    </section>
  );
}
