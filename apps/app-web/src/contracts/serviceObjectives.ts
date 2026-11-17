import { isHeadScheduledFromContract } from "./environmentContract.generated";
import { universeIdHasRole } from "./universes";

export const ACCEPTED_SERVICE_OBJECTIVES = {
  minimum_availability: 0.95,
  minimum_corroborated_share: 0.95,
} as const;

export type ServiceObjectives = typeof ACCEPTED_SERVICE_OBJECTIVES;

export type HeadFreshness = "fresh" | "stale" | "manual" | "unknown";

export const SWEEP_RUN_SOURCE_REGEX_SOURCE = "-[0-9]{8}T[0-9]{4}$";
export const SWEEP_RUN_SOURCE_PATTERN = new RegExp(SWEEP_RUN_SOURCE_REGEX_SOURCE);

export const REGISTERED_RAW_SOURCE_FILTER =
  `not (source ~ '${SWEEP_RUN_SOURCE_REGEX_SOURCE}')`;

export const UTC_DAY_START_SQL =
  "(date_trunc('day', now() at time zone 'utc') at time zone 'utc')";

export const NON_CANARY_UNIVERSE_WHERE = "s.universe_id not like all ($1::text[])";

export function isCanaryUniverse(universeId: string): boolean {
  return universeIdHasRole(universeId, "sentinel");
}

export const isHeadScheduled = isHeadScheduledFromContract;

export function resolveHeadFreshness(
  rawFreshness: string | null | undefined,
  universeId: string,
  environment: string | null | undefined,
): HeadFreshness {
  const scheduled = isHeadScheduled(universeId, environment);
  if (!scheduled) {
    return "manual";
  }
  if (
    rawFreshness === "fresh" ||
    rawFreshness === "stale" ||
    rawFreshness === "manual"
  ) {
    return rawFreshness;
  }
  return "unknown";
}

