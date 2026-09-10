/**
 * The reader for module 6 (#772, init.md §0 question 6): "who is the purest name under a
 * given theme".
 *
 * Every number here is a column. The share, the denominator it is over, and the three
 * revenue masses are `factors.base.theme_purity`'s outputs materialized into
 * `mart.issuer_theme_purity` by the weekly standards lane. init.md §1 rule 2 confines this
 * layer to deterministic within-row reformatting, and a purity is a ratio across a set of
 * segment rows — a metric, and metrics live in libs/factors.
 *
 * The one thing this file must not do is rank on something it computed. It orders by the
 * stored `theme_share` and shows `unclassifiedRevenue` beside it, because a share of 0.60
 * with 0.35 unclassified and one with 0.00 unclassified are different claims and a page that
 * cannot tell them apart is ranking its own classifier coverage.
 *
 * Refused rows are rendered, not hidden. A `null` share with `below_minimum_classified_share`
 * is the honest state for an issuer whose segments the classifier could not read enough of,
 * and dropping it from the list would make the ranking look more complete than it is.
 */

import { withMartReadonly } from "./db";
import type { MartClientLike } from "./topt-gppe-repository";

export type ThemePurityRow = {
  issuerId: string;
  /** Null when the factor REFUSED the share — see reasonCodes. Never a zero, which would
   * read as "none of this issuer is in the theme" rather than "we could not say". */
  themeShare: string | null;
  consolidatedRevenue: string;
  inThemeRevenue: string;
  outOfThemeRevenue: string;
  unclassifiedRevenue: string;
  partitionResidual: string;
  segments: number;
  confidence: string;
  reasonCodes: string[];
  /** The served model and prompt digest that judged the segments, or a rule id. */
  extractor: string;
  availabilityStatus: string;
  sourceEvidenceStatus: string;
  factorValidationStatus: string;
  periodEnd: string;
};

export type ThemePurityGroup = {
  themeId: string;
  theme: string;
  definitionVersion: string;
  definitionSha256: string;
  cutoff: string;
  runId: string;
  rows: ThemePurityRow[];
};

/**
 * The newest run that materialized purity rows. Read from the rows themselves rather than
 * from the pointer: the weekly lane stamps each row with the governed head it computed
 * against, so "the newest run present here" IS a governed run — and asking the pointer
 * instead would name a run that may have no purity rows at all (the lane had not run yet),
 * leaving the page empty while rows existed.
 */
const LATEST_RUN_SQL = `
  select run_id, max(cutoff) as cutoff
  from mart.issuer_theme_purity
  group by run_id
  order by max(cutoff) desc
  limit 1
`;

/** Ordered by the stored share, refusals last — the database's ordering, not this layer's. */
const ROWS_SQL = `
  select theme_id,
         theme,
         definition_version,
         definition_sha256,
         to_char(cutoff, 'YYYY-MM-DD"T"HH24:MI:SSOF') as cutoff,
         issuer_id,
         theme_share::text as theme_share,
         consolidated_revenue::text as consolidated_revenue,
         in_theme_revenue::text as in_theme_revenue,
         out_of_theme_revenue::text as out_of_theme_revenue,
         unclassified_revenue::text as unclassified_revenue,
         partition_residual::text as partition_residual,
         segments,
         confidence::text as confidence,
         reason_codes,
         extractor,
         availability_status,
         source_evidence_status,
         factor_validation_status,
         to_char(period_end, 'YYYY-MM-DD') as period_end
  from mart.issuer_theme_purity
  where run_id = $1
  order by theme_id, theme_share desc nulls last, issuer_id
`;

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export async function loadThemePurity(
  runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
): Promise<ThemePurityGroup[]> {
  return runWithClient(async (client) => {
    const head = await client.query(LATEST_RUN_SQL);
    const runId = head.rows.length > 0 ? text(head.rows[0].run_id) : "";
    if (runId === "") return [];

    const result = await client.query(ROWS_SQL, [runId]);
    const groups = new Map<string, ThemePurityGroup>();
    for (const row of result.rows) {
      const themeId = text(row.theme_id);
      let group = groups.get(themeId);
      if (!group) {
        group = {
          themeId,
          theme: text(row.theme),
          definitionVersion: text(row.definition_version),
          definitionSha256: text(row.definition_sha256),
          cutoff: text(row.cutoff),
          runId,
          rows: [],
        };
        groups.set(themeId, group);
      }
      group.rows.push({
        issuerId: text(row.issuer_id),
        themeShare: typeof row.theme_share === "string" ? row.theme_share : null,
        consolidatedRevenue: text(row.consolidated_revenue),
        inThemeRevenue: text(row.in_theme_revenue),
        outOfThemeRevenue: text(row.out_of_theme_revenue),
        unclassifiedRevenue: text(row.unclassified_revenue),
        partitionResidual: text(row.partition_residual),
        segments: typeof row.segments === "number" ? row.segments : Number(row.segments ?? 0),
        confidence: text(row.confidence),
        reasonCodes: Array.isArray(row.reason_codes) ? (row.reason_codes as string[]) : [],
        extractor: text(row.extractor),
        availabilityStatus: text(row.availability_status),
        sourceEvidenceStatus: text(row.source_evidence_status),
        factorValidationStatus: text(row.factor_validation_status),
        periodEnd: text(row.period_end),
      });
    }
    return [...groups.values()];
  });
}

/**
 * The share of the denominator that carries a judgement either way, as a 0-1 string.
 *
 * Deterministic within-row reformatting of columns the factor already wrote — rule 2's
 * permitted shape, and the reason it is computed here rather than stored: it is
 * `(in + out) / consolidated` on one row, with no other row involved.
 */
export function classifiedShare(row: ThemePurityRow): string | null {
  const total = Number(row.consolidatedRevenue);
  if (!Number.isFinite(total) || total <= 0) return null;
  const judged = Number(row.inThemeRevenue) + Number(row.outOfThemeRevenue);
  if (!Number.isFinite(judged)) return null;
  return (judged / total).toFixed(4);
}
