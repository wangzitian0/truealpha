/**
 * #1255: the macro base data loader for /admin/macro. Administrator-only.
 *
 * It reads three mart relations through `mart_readonly` and computes nothing:
 *
 *   mart.macro_series_status   one row per series: the newest observation, its source and fetch time,
 *                              its age in calendar days and the limit that its registration declares
 *                              (`max_age_days`), and the verdict `is_fresh`. The view states the
 *                              freshness; the nightly verdict `macro_freshness` reads the same view.
 *   mart.macro_series_current  the newest vintage of each observation date, for the last 12 months.
 *
 * Values stay exact `numeric` strings. The page formats them for display and keeps the exact string
 * in a `title` attribute.
 */

import { withMartReadonly } from "@/server/mart/db";
import type { ReadState } from "@/server/read-state";

export interface MacroPrincipal {
  principalKind: "member" | "administrator" | "service";
}

export interface MacroLatest {
  observationDate: string;
  value: string;
  source: string;
  fetchedAt: string;
  ageDays: number;
}

export interface MacroSeries {
  seriesId: string;
  label: string;
  unit: string;
  sourceId: string;
  maxAgeDays: number;
  /** `null` when the series has no observation yet. */
  latest: MacroLatest | null;
  isFresh: boolean;
}

export interface MacroObservationRow {
  seriesId: string;
  observationDate: string;
  value: string;
}

export interface MacroPanel {
  series: MacroSeries[];
  /** The newest vintage of each date of the last 12 months, newest date first. */
  months: MacroObservationRow[];
}

export type MacroOutcome = ReadState<MacroPanel>;

export const STATUS_SQL =
  "select series_id, label, unit, source_id, max_age_days, observation_date::text as observation_date, " +
  "value::text as value, source, fetched_at, age_days, is_fresh from mart.macro_series_status order by series_id";

export const MONTHS_SQL =
  "select series_id, observation_date::text as observation_date, value::text as value " +
  "from mart.macro_series_current " +
  "where observation_date >= ((now() at time zone 'UTC')::date - interval '12 months')::date " +
  "order by observation_date desc, series_id";

/** The SQLSTATE of "relation does not exist": the database predates the migration of #1255. */
const UNDEFINED_TABLE = "42P01";

function text(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

/** The SQL casts dates to text, so the driver never builds a Date in the server's time zone. */
function day(value: unknown): string {
  return String(value).slice(0, 10);
}

export async function loadMacroPanel(principal: MacroPrincipal | null): Promise<MacroOutcome> {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }
  try {
    const { statusRows, monthRows } = await withMartReadonly(async (client) => {
      const status = await client.query(STATUS_SQL);
      const months = await client.query(MONTHS_SQL);
      return { statusRows: status.rows, monthRows: months.rows };
    });
    if (statusRows.length === 0) return { kind: "empty" };
    const series: MacroSeries[] = statusRows.map((row) => ({
      seriesId: String(row.series_id),
      label: String(row.label),
      unit: String(row.unit),
      sourceId: String(row.source_id),
      maxAgeDays: Number(row.max_age_days),
      latest:
        row.observation_date === null || row.observation_date === undefined
          ? null
          : {
              observationDate: day(row.observation_date),
              value: text(row.value),
              source: String(row.source),
              fetchedAt: text(row.fetched_at),
              ageDays: Number(row.age_days),
            },
      isFresh: row.is_fresh === true,
    }));
    const months: MacroObservationRow[] = monthRows.map((row) => ({
      seriesId: String(row.series_id),
      observationDate: day(row.observation_date),
      value: text(row.value),
    }));
    return { kind: "ready", data: { series, months } };
  } catch (error) {
    if (typeof error === "object" && error !== null && (error as { code?: unknown }).code === UNDEFINED_TABLE) {
      return { kind: "unavailable", reason: "the macro series tables are not in this database yet" };
    }
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
