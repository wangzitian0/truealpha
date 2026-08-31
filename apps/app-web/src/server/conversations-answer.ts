/**
 * The first assistant reply (#46 v0, B-phase /chat minimal loop): a user
 * prompt gets a deterministic, provenance-carrying answer from the governed
 * mart readers — or an honest `unsupported`.
 *
 * Deliberately NOT intelligence: intent is a stated keyword match, the answer
 * is a read of already-materialized planes (the same loadFundValuation the
 * holdings page renders), and everything else says so via the `unsupported`
 * outcome #396's enum shipped for exactly this constrained v1. Rich intent
 * extraction stays #46's scope. This module DECIDES; conversations.ts stays
 * pure storage (its own boundary comment).
 */

import type { FundValuation } from "@/server/mart/fund-valuation";
import { loadFundValuation } from "@/server/mart/fund-valuation";
import type { ConversationOutcome } from "@/server/conversations";

export type ComposedAnswer = { content: string; outcome: ConversationOutcome };

const HOLDINGS_INTENT = /holding|weight|constituen|portfolio|持仓|权重|成分/i;

export function composeHoldingsAnswer(funds: FundValuation[]): ComposedAnswer {
  if (funds.length === 0) {
    return {
      content: "No fund holdings have been captured yet — the weekly N-PORT refresh lands the first vintage.",
      outcome: "unavailable",
    };
  }
  const sections = funds.map((fund) => {
    const top = fund.lines
      .slice(0, 5)
      .map((line) => `${line.holdingName} ${line.weightPct === null ? "—" : Number(line.weightPct).toFixed(2)}%`)
      .join(", ");
    const run = fund.runId ? `governed run ${fund.runId}` : "no governed valuation run yet";
    return (
      `${fund.fundName} (period ${fund.reportPeriod}): top filed weights — ${top}. ` +
      `Coverage: filed ${fund.totalWeightPct}%, resolved ${fund.resolvedWeightPct}%, ` +
      `valued ${fund.valuedWeightPct}% (${run}).`
    );
  });
  return {
    content: `${sections.join("\n")}\nFull table: /research/holdings — the fund's own filed N-PORT weights.`,
    outcome: "result",
  };
}

export function unsupportedAnswer(): ComposedAnswer {
  return {
    content:
      "This assistant currently answers one question class: fund holdings and their valuation coverage " +
      '(ask about "holdings" or "weights"). Richer questions are tracked on #46 and will say so here when they land.',
    outcome: "unsupported",
  };
}

/** Deterministic intent → governed read → composed answer. Exported seam takes
 * the loader so tests inject; the action passes nothing. */
export async function answerUserPrompt(
  content: string,
  loadValuation: () => Promise<FundValuation[]> = loadFundValuation,
): Promise<ComposedAnswer> {
  if (HOLDINGS_INTENT.test(content)) {
    return composeHoldingsAnswer(await loadValuation());
  }
  return unsupportedAnswer();
}
