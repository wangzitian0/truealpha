/**
 * Structural mirror of `truealpha_contracts.topt_read` (Python) — see #433.
 *
 * `ToptGppeReport`/`ToptGppeCell`/`ToptGppeUnavailable` mirror the Pydantic models
 * field-for-field (including snake_case) so the App and the MCP `topt_gppe` tool
 * return semantically identical responses for the same governed run, the same
 * convention `strategyRun.ts` already follows for the Core Strategy DTOs.
 */

export interface ToptGppeCell {
  listing_id: string;
  availability: string;
  gppe: string | null;
  confidence: string | null;
}

export interface ToptGppeReport {
  run_id: string;
  requested_count: number;
  available_count: number;
  cells: readonly ToptGppeCell[];
  quality: Record<string, unknown> | null;
}

export interface ToptGppeUnavailable {
  reason: string;
}
