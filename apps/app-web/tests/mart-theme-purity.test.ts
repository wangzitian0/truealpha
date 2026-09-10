/**
 * loadThemePurity against a fake mart client (#772, init.md §0 question 6).
 *
 * The reader's whole job is to not become a second implementation. What it must prove:
 *
 *  - the share is READ, never recomputed — a page that divided its own numerator by its own
 *    denominator would be a metric in the App layer (init.md §1 rule 2), and would be free
 *    to pick the wrong denominator all over again;
 *  - a REFUSED row is rendered, not dropped. Hiding it would make the ranking look more
 *    complete than the classifier's coverage actually was;
 *  - `classifiedShare` is deterministic within-row reformatting of columns the factor
 *    already wrote — rule 2's permitted shape, and nothing else in this file computes.
 */

import { classifiedShare, loadThemePurity } from "../src/server/mart/theme-purity";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const HEAD = [{ run_id: "run-9", cutoff: "2026-09-13T09:07:00+00" }];

/** AVGO's real shape, plus an issuer the classifier could not read enough of. */
const ROWS = [
  {
    theme_id: "ai-infrastructure",
    theme: "AI infrastructure",
    definition_version: "v0",
    definition_sha256: "a".repeat(64),
    cutoff: "2026-09-13T09:07:00+00:00",
    issuer_id: "issuer:cik:0001730168",
    theme_share: "0.5769248829965407672922503796",
    consolidated_revenue: "63887000000",
    in_theme_revenue: "36858000000",
    out_of_theme_revenue: "27029000000",
    unclassified_revenue: "0",
    partition_residual: "0",
    segments: 2,
    confidence: "0.85",
    reason_codes: [],
    extractor: "model:glm-5.3:abcdef123456",
    availability_status: "available",
    source_evidence_status: "verified",
    factor_validation_status: "not_evaluated",
    period_end: "2025-11-02",
  },
  {
    theme_id: "ai-infrastructure",
    theme: "AI infrastructure",
    definition_version: "v0",
    definition_sha256: "a".repeat(64),
    cutoff: "2026-09-13T09:07:00+00:00",
    issuer_id: "issuer:cik:0000000002",
    theme_share: null,
    consolidated_revenue: "1000",
    in_theme_revenue: "100",
    out_of_theme_revenue: "0",
    unclassified_revenue: "900",
    partition_residual: "0",
    segments: 2,
    confidence: "0",
    reason_codes: ["unclassified_revenue", "below_minimum_classified_share"],
    extractor: "model:glm-5.3:abcdef123456",
    availability_status: "unavailable",
    source_evidence_status: "degraded",
    factor_validation_status: "not_evaluated",
    period_end: "2025-12-31",
  },
  {
    theme_id: "semiconductors",
    theme: "Semiconductors",
    definition_version: "v0",
    definition_sha256: "b".repeat(64),
    cutoff: "2026-09-13T09:07:00+00:00",
    issuer_id: "issuer:cik:0001730168",
    theme_share: "0.5769248829965407672922503796",
    consolidated_revenue: "63887000000",
    in_theme_revenue: "36858000000",
    out_of_theme_revenue: "27029000000",
    unclassified_revenue: "0",
    partition_residual: "0",
    segments: 2,
    confidence: "0.85",
    reason_codes: [],
    extractor: "model:glm-5.3:abcdef123456",
    availability_status: "available",
    source_evidence_status: "verified",
    factor_validation_status: "not_evaluated",
    period_end: "2025-11-02",
  },
];

function client(head = HEAD, rows = ROWS): { client: MartClientLike; queries: string[]; params: unknown[][] } {
  const queries: string[] = [];
  const params: unknown[][] = [];
  return {
    queries,
    params,
    client: {
      async query(sql: string, values?: readonly unknown[]) {
        queries.push(sql);
        params.push([...(values ?? [])]);
        if (sql.includes("group by run_id")) return { rows: head as Record<string, unknown>[] };
        return { rows: rows as Record<string, unknown>[] };
      },
    },
  };
}

async function run() {
  // The share is read verbatim, at full precision — no rounding, no recomputation.
  {
    const { client: fake } = client();
    const groups = await loadThemePurity((fn) => fn(fake));
    assert(groups.length === 2, "one group per theme");
    const ai = groups.find((g) => g.themeId === "ai-infrastructure");
    assert(ai, "the ai-infrastructure group is present");
    assert(ai.rows.length === 2, "both issuers, including the refused one");
    assert(
      ai.rows[0].themeShare === "0.5769248829965407672922503796",
      "the stored share is passed through untouched",
    );
    assert(ai.rows[0].consolidatedRevenue === "63887000000", "and so is the denominator it is over");
  }

  // A refused row survives the read. Dropping it would hide the classifier's coverage gap.
  {
    const { client: fake } = client();
    const groups = await loadThemePurity((fn) => fn(fake));
    const refused = groups[0].rows.find((row) => row.issuerId === "issuer:cik:0000000002");
    assert(refused, "the refused issuer is still in the list");
    assert(refused.themeShare === null, "refused reads as null, never as zero");
    assert(
      refused.reasonCodes.includes("below_minimum_classified_share"),
      "and it says why",
    );
    assert(refused.unclassifiedRevenue === "900", "with the mass that was never judged");
  }

  // Only one run's rows are read, and it is named by the rows themselves.
  {
    const { client: fake, params } = client();
    await loadThemePurity((fn) => fn(fake));
    assert(params[1][0] === "run-9", "the row query is scoped to the newest run");
  }

  // No rows yet is empty, not an error: the weekly lane has simply not written any.
  {
    const { client: fake } = client([], []);
    const groups = await loadThemePurity((fn) => fn(fake));
    assert(groups.length === 0, "no run means no groups");
  }

  // classifiedShare is within-row arithmetic over columns the factor wrote — over the READ
  // rows, so the test exercises the same shape the page hands it.
  {
    const { client: fake } = client();
    const groups = await loadThemePurity((fn) => fn(fake));
    const [full, partial] = groups[0].rows;
    assert(classifiedShare(full) === "1.0000", "AVGO is fully judged");
    assert(classifiedShare(partial) === "0.1000", "the refused issuer is 10% judged");
    assert(
      classifiedShare({ ...full, consolidatedRevenue: "0" }) === null,
      "a non-positive denominator has no classified share, rather than an infinity",
    );
  }

  console.log("mart-theme-purity: ok");
}

await run();
