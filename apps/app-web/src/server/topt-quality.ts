import type { AccessContext } from "@/contracts/strategyRun";
import type { ToptGppeReport, ToptGppeUnavailable } from "@/contracts/toptGppe";
import type { ReadState } from "@/server/dashboard";
import { MartToptGppeRepository } from "@/server/mart/topt-gppe-repository";

export interface ToptGppeRepositoryLike {
  latest(limit?: number): Promise<ToptGppeReport | ToptGppeUnavailable>;
}

export function captureAvailableCount(report: ToptGppeReport): number | null {
  const value = report.quality?.available_count;
  const usable =
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= report.requested_count;
  return usable ? value : null;
}

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
