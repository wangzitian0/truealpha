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
