/**
 * #494: the L0–L5 quality funnel loader (init.md §2.1's layers as one
 * diagnostic). Administrator-only. Each layer reports ONE governing metric
 * and a status; the funnel's SHAPE is the diagnosis — the layer where the
 * bar narrows is where data is dying. Sources:
 *
 *   L0 sources   — per-source fetches today (raw.fetches via app_ops_reader)
 *   L1 capture   — persisted quality report (availability/freshness)
 *   L2 facts     — mart.strategy_input_coverage (#496 metric)
 *   L3 factors   — GPPE availability per listing (mart read)
 *   L4 strategy  — decision waterfall from the latest run
 *   L5 consume   — governed pointer age (mart.current_pointer_head)
 */

import { withMartReadonly } from "@/server/mart/db";
import { MartStrategyRunRepository } from "@/server/mart/strategy-run-repository";
import { MartToptGppeRepository } from "@/server/mart/topt-gppe-repository";
import type { AccessContext } from "@/contracts/strategyRun";

export interface FunnelLayer {
  key: string;
  title: string;
  headline: string;
  ratio: number | null; // 0..1 drives the bar width; null renders full-width muted
  status: "ok" | "warn" | "crit" | "muted";
  detail: string;
}

export interface FunnelPrincipal {
  context: AccessContext;
  principalKind: "member" | "administrator" | "service";
}

export type FunnelOutcome =
  | { kind: "ready"; runId: string; layers: FunnelLayer[] }
  | { kind: "denied" }
  | { kind: "unavailable"; reason: string }
  | { kind: "error"; message: string };

function num(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function loadQualityFunnel(principal: FunnelPrincipal | null): Promise<FunnelOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }
  try {
    const gppe = await new MartToptGppeRepository().latest();
    if (!("cells" in gppe)) return { kind: "unavailable", reason: gppe.reason };
    const runId = gppe.run_id;
    const quality = (gppe.quality ?? {}) as Record<string, unknown>;

    const [coverage, pointerAgeHours] = await withMartReadonly(async (client) => {
      const cov = await client.query(
        "select count(*)::int as total, sum((present_count = required_count)::int)::int as complete " +
          "from mart.strategy_input_coverage where run_id = $1",
        [runId],
      );
      const ptr = await client.query(
        "select extract(epoch from (now() - advanced_at)) / 3600.0 as age " +
          "from mart.current_pointer_head order by sequence desc limit 1",
      );
      return [
        { total: Number(cov.rows[0]?.total ?? 0), complete: Number(cov.rows[0]?.complete ?? 0) },
        ptr.rows.length === 0 ? null : Number(ptr.rows[0].age),
      ] as const;
    });

    const strategy = await new MartStrategyRunRepository().getLatest(
      "large_model_value_v0",
      principal.context,
    );
    const decisions = "decisions" in strategy ? strategy.decisions : [];
    const selected = decisions.filter((d) => d.outcome === "selected").length;
    const ranked = decisions.filter((d) => d.rank !== null).length;

    const availability = num(quality.availability);
    const freshness = num(quality.freshness);
    const gppeAvailable = gppe.cells.filter((cell) => cell.availability === "available").length;

    const layers: FunnelLayer[] = [
      {
        key: "L1",
        title: "Capture",
        headline: `${num(quality.available_count) ?? "?"} / ${num(quality.requested_count) ?? "?"} cells`,
        ratio: availability,
        status: availability === null ? "muted" : availability >= 1 ? "ok" : availability >= 0.9 ? "warn" : "crit",
        detail: `freshness ${freshness ?? "?"} · reconciliation ${num(quality.independent_reconciliation) ?? "?"}`,
      },
      {
        key: "L2",
        title: "Facts",
        headline:
          coverage.total === 0
            ? "no coverage rows yet"
            : `${coverage.complete} / ${coverage.total} issuers input-complete`,
        ratio: coverage.total === 0 ? null : coverage.complete / coverage.total,
        status:
          coverage.total === 0
            ? "muted"
            : coverage.complete === coverage.total
              ? "ok"
              : coverage.complete / coverage.total >= 0.75
                ? "warn"
                : "crit",
        detail: "missing_keys per issuer in mart.strategy_input_coverage (#496 fix-list)",
      },
      {
        key: "L3",
        title: "Factors",
        headline: `GPPE ${gppeAvailable} / ${gppe.cells.length} listings`,
        ratio: gppe.cells.length === 0 ? null : gppeAvailable / gppe.cells.length,
        status:
          gppe.cells.length === 0
            ? "muted"
            : gppeAvailable === gppe.cells.length
              ? "ok"
              : gppeAvailable / gppe.cells.length >= 0.75
                ? "warn"
                : "crit",
        detail: "bounded by L2 — per-listing table below",
      },
      {
        key: "L4",
        title: "Strategy",
        headline: `${decisions.length} evaluated → ${ranked} ranked → ${selected} selected`,
        ratio: decisions.length === 0 ? null : Math.max(selected / Math.max(decisions.length, 1), 0.04),
        status: decisions.length === 0 ? "crit" : "ok",
        detail: "claim ceiling: preview — selection narrowing is by design, not data loss",
      },
      {
        key: "L5",
        title: "Consumption",
        headline: pointerAgeHours === null ? "no pointer" : `pointer ${pointerAgeHours.toFixed(1)}h old`,
        ratio: pointerAgeHours === null ? null : Math.max(0.04, Math.min(1, 1 - pointerAgeHours / 48)),
        status: pointerAgeHours === null ? "crit" : pointerAgeHours <= 26 ? "ok" : "warn",
        detail: "mart.current_pointer_head — web/MCP/chat resolve the same governed run",
      },
    ];
    return { kind: "ready", runId, layers };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
