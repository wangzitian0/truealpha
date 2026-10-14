/**
 * #1235 criterion 1, App half: the Today page (/research) and /research/strategy name the
 * definition of the served run, and a provisional definition reads PROVISIONAL.
 *
 * Production serves runs of definition 0.1.0, whose tier bands no one reviewed. Staging serves
 * the reviewed 1.0.0. Before this marker the two pages looked the same.
 *
 * Each case records the newest run inside one transaction on the test's own client. The
 * client goes to the repository through db.ts `__setTestClient`, and the test rolls it back.
 * The read goes through `loadStrategyRunPage` with its default repository: the call that
 * both pages make. The test then renders `StrategyDefinitionLine`, the component that both
 * pages render, with the exact `definition` prop that the pages pass. It does not render the
 * whole page: the pages read the session cookie through next/headers, which works only in a
 * Next.js request.
 *
 * Run standalone: `bun run tests/strategy-definition-marker.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { Client, type PoolClient } from "pg";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { StrategyDefinitionLine } from "../src/components/strategy-definition";
import type { AccessContext } from "../src/contracts/strategyRun";
import { __setTestClient } from "../src/server/mart/db";
import { loadStrategyRunPage, type StrategyRunPrincipal } from "../src/server/strategy-page";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const dbUrl = requireDb("strategy-definition-marker", { write: true });
if (!dbUrl) {
  process.exit(0);
}
const DATABASE_URL: string = dbUrl;
process.env.DATABASE_URL = DATABASE_URL;

const CONTEXT: AccessContext = {
  contextId: "ctx:definition-marker",
  principalId: "principal:definition-marker",
  tenantId: "tenant:definition-marker",
  sessionId: "session:definition-marker",
  authenticationMethod: "password",
  issuedAt: "2026-10-14T00:00:00Z",
  expiresAt: "2026-10-14T01:00:00Z",
};
const MEMBER: StrategyRunPrincipal = { context: CONTEXT, principalKind: "member" };

const hex64 = () => randomBytes(32).toString("hex");

/** The two definitions as the review record states them
 * (governance/reviews/large_model_value_v0-definition-1.0.0.md). Written out here as an
 * oracle; the generated registry under test does not supply them. */
const CASES = [
  {
    name: "definition 0.1.0 (production today)",
    version: "0.1.0",
    sha256: "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d",
    state: "provisional",
    text: "Definition 0.1.0 · PROVISIONAL: bands not reviewed",
    provisional: true,
  },
  {
    name: "definition 1.0.0 (staging today)",
    version: "1.0.0",
    sha256: "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803",
    state: "reviewed",
    text: "Definition 1.0.0 · reviewed",
    provisional: false,
  },
  {
    name: "a reviewed version string with a hash outside the registry",
    version: "1.0.0",
    sha256: "f".repeat(64),
    state: "unknown",
    text: "Definition unknown · not reviewed",
    provisional: false,
  },
] as const;

/** The visible text of a static render: the markup without its tags. */
function visibleText(html: string): string {
  return html.replace(/<[^>]*>/g, "");
}

const admin = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
await admin.connect();
try {
  await admin.query("begin");
  __setTestClient(admin as unknown as Pick<PoolClient, "query">);

  for (const [index, testCase] of CASES.entries()) {
    // Each case is the newest run: later than the case before it, and later than every
    // run another test commits.
    const runId = `strategy-run:${hex64()}`;
    await admin.query(
      `insert into mart.strategy_runs
         (strategy_run_id, content_sha256, strategy_key, strategy_version,
          definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
       values ($1, $2, 'large_model_value_v0', $3, $4, $5, 'preview', $6)`,
      [runId, hex64(), testCase.version, testCase.sha256, hex64(), `2028-01-0${index + 1}T00:00:00Z`],
    );
    await admin.query(
      `insert into mart.strategy_decisions
         (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at, eligible, outcome)
       values ($1, $2, $3, 'issuer:definition-marker', '2026-06-30T23:59:59Z', true, 'selected')`,
      [`strategy-decision:${hex64()}`, hex64(), runId],
    );

    const outcome = await loadStrategyRunPage(MEMBER, "large_model_value_v0");
    assert(outcome.kind === "ready", `${testCase.name}: expected a ready page, got ${JSON.stringify(outcome)}`);
    assert(
      outcome.report.strategy_run_id === runId,
      `${testCase.name}: the page read ${outcome.report.strategy_run_id}, not the seeded run`,
    );
    const definition = outcome.report.definition;
    assert(
      definition?.version === testCase.version &&
        definition.content_sha256 === testCase.sha256 &&
        definition.review_state === testCase.state,
      `${testCase.name}: the served report carries ${JSON.stringify(definition)}`,
    );

    const html = renderToStaticMarkup(createElement(StrategyDefinitionLine, { definition }));
    const text = visibleText(html);
    assert(text === testCase.text, `${testCase.name}: the page line reads "${text}", not "${testCase.text}"`);
    assert(
      html.includes(`data-review-state="${testCase.state}"`),
      `${testCase.name}: the line does not state data-review-state="${testCase.state}": ${html}`,
    );
    assert(
      text.includes("PROVISIONAL") === testCase.provisional,
      `${testCase.name}: PROVISIONAL must appear exactly when the bands are provisional: "${text}"`,
    );
  }

  // A report without a marker never shows a review that it does not carry.
  const withoutMarker = visibleText(renderToStaticMarkup(createElement(StrategyDefinitionLine, { definition: undefined })));
  assert(
    withoutMarker === "Definition unknown · not reviewed",
    `a report without a definition reads "${withoutMarker}"`,
  );

  console.log(`#1235 strategy definition marker: ${CASES.length} served runs and the absent marker passed`);
} finally {
  __setTestClient(null);
  await admin.query("rollback").catch(() => {});
  await admin.end().catch(() => {});
}
