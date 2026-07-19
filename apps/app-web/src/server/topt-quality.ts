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
