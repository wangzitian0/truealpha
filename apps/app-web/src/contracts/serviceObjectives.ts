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
