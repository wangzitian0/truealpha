/**
 * #773 / #1213: Declared service objectives for DataHub governed heads (#1062 / docs/datahub-service-demand.md).
 * SSOT alignment with `ACCEPTED_SERVICE_OBJECTIVES` in `apps/data-engine/src/data_engine/datahub/a1_evidence.py`.
 *
 * Every number here is quoted from docs/datahub-service-demand.md:
 * - 95% availability floor
 * - 95% minimum corroborated share (independent origins consensus)
 */

export const ACCEPTED_SERVICE_OBJECTIVES = {
  minimum_availability: 0.95,
  minimum_corroborated_share: 0.95,
} as const;

export type ServiceObjectives = typeof ACCEPTED_SERVICE_OBJECTIVES;

/**
 * #1362: Canonical filter excluding legacy run-suffixed source strings (such as `yfinance-20260725T0527`)
 * from operational raw-fetch inventories.
 */
export const REGISTERED_RAW_SOURCE_FILTER =
  "not (source ~ '-[0-9]{8}T[0-9]{4}$')";

/**
 * #1362: Synthetic canary universes validate pipeline liveness and do not run multi-vendor
 * price corroboration (matching NON_CANARY_UNIVERSE_WHERE in datahub-stats.ts).
 */
export function isCanaryUniverse(universeId: string): boolean {
  return (
    universeId.startsWith("canary") || universeId.startsWith("universe:canary")
  );
}

/**
 * #1318 / #1359 / #1362: Mirrors `is_head_scheduled` in `apps/data-engine/src/data_engine/lanes/capture.py`.
 * `qqq_live_schedule` runs in production only; in staging, `universe:qqq-%` heads are manual.
 */
export function isHeadScheduled(
  universeId: string,
  environment: string | null | undefined,
): boolean {
  const env = (environment ?? "production").trim().toLowerCase();
  if (env === "staging" && universeId.startsWith("universe:qqq-")) {
    return false;
  }
  return true;
}

