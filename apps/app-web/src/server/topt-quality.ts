/**
 * Server-only route loader for the TOPT GPPE / data-quality page — see #433.
 *
 * Reads the same governed production run the MCP `topt_gppe` tool serves, through
 * `MartToptGppeRepository` (real `mart` reads, `mart_readonly` role — never a
 * fixture; #429/AGENTS.md rule 6). Takes an already-resolved `AccessContext | null`,
 * same pattern as `dashboard.ts`'s loaders: this module never derives identity
 * itself, and a `null` context is `denied` before the repository is touched.
 */

import type { AccessContext } from "@/contracts/strategyRun";
import type { ToptGppeReport, ToptGppeUnavailable } from "@/contracts/toptGppe";
import type { ReadState } from "@/server/dashboard";
import { MartToptGppeRepository } from "@/server/mart/topt-gppe-repository";

export interface ToptGppeRepositoryLike {
  latest(limit?: number): Promise<ToptGppeReport | ToptGppeUnavailable>;
}

/**
 * The capture-plane available-cell count from the run's own quality report, or
 * `null` when the report is absent or malformed — the caller then omits the
 * capture-plane clause instead of rendering a wrong number.
 *
 * Exists because the quality page rendered `report.available_count` (the MART
 * side's listing-level count, scale /20) against `requested_count` (the capture
 * plane's cell denominator, scale /84) — "18 / 84 cells available" while the
 * funnel one line above correctly said 82/84. Two denominators, one sentence
 * (#539; observed on the production page 2026-08-15). The cell-level numerator
 * lives in the report's quality payload, written by the same run.
 */
export function captureAvailableCount(report: ToptGppeReport): number | null {
  const value = report.quality?.available_count;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** `repository` is an injection point for tests only; production callers omit it. */
export async function loadToptQuality(
  context: AccessContext | null,
  repository: ToptGppeRepositoryLike = new MartToptGppeRepository(),
): Promise<ReadState<ToptGppeReport>> {
  if (context === null) return { kind: "denied" };

  try {
    const result = await repository.latest();
    if ("cells" in result) return { kind: "ready", data: result };
    return { kind: "unavailable", reason: result.reason };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
