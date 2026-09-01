/**
 * #46 v0: the deterministic reply loop — holdings intent reads the governed
 * valuation loader and answers with provenance and coverage; everything else
 * is an honest `unsupported`; an empty plane is `unavailable`, not a guess.
 */

import { answerUserPrompt, composeHoldingsAnswer } from "../src/server/conversations-answer";
import type { FundValuation } from "../src/server/mart/fund-valuation";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const FUND: FundValuation = {
  fundId: "etf:series:S1",
  fundName: "Invesco QQQ Trust",
  reportPeriod: "2026-06-30",
  runId: "capture-run:abc123",
  valuedWeightPct: "48.78",
  resolvedWeightPct: "99.70",
  totalWeightPct: "99.93",
  weightedGap: "-0.31",
  lines: [
    { holdingName: "NVIDIA Corp.", ticker: "NVDA", weightPct: "7.60", currentPs: "20.1", targetPsMidpoint: "18.0", valuationGap: "-0.10", tier: "tier-1", availability: "available" },
    { holdingName: "Apple Inc.", ticker: "AAPL", weightPct: "6.67", currentPs: null, targetPsMidpoint: null, valuationGap: null, tier: null, availability: "unavailable" },
  ],
};

{
  const answer = await answerUserPrompt("what are the holdings weights?", async () => [FUND]);
  assert(answer.outcome === "result", "holdings intent answers with a result");
  assert(answer.content.includes("NVIDIA Corp. 7.60%"), "top weights are the fund's filed numbers");
  assert(answer.content.includes("valued 48.78%"), "coverage mass is stated, not implied");
  assert(answer.content.includes("capture-run:abc123"), "the governed run rides as provenance");
}

{
  const answer = await answerUserPrompt("持仓权重怎么样", async () => [FUND]);
  assert(answer.outcome === "result", "the intent matches Chinese phrasing too");
}

{
  const answer = await answerUserPrompt("predict tomorrow's price", async () => [FUND]);
  assert(answer.outcome === "unsupported", "everything else is honestly unsupported");
  assert(answer.content.includes("#46"), "the unsupported answer names where richer intent lives");
}

{
  const answer = composeHoldingsAnswer([]);
  assert(answer.outcome === "unavailable", "an empty plane is unavailable, never a fabricated answer");
}

console.log("conversations answer loop passed");
