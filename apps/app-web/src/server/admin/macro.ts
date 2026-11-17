import { timestampText } from "@/client/coerce";
import { withMartReadonly } from "@/server/mart/db";
import type { ReadState } from "@/server/read-state";
import { isUndefinedTable, runAdminRead, type OpsPrincipal } from "./ops";

export type MacroPrincipal = OpsPrincipal;

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

// The date comes out as `observation_day`, formatted by `to_char`. So the date text does not depend on the
// DateStyle of the server. The `order by` names the column `observation_date`, never the text.
export const STATUS_SQL =
  "select series_id, label, unit, source_id, max_age_days, to_char(observation_date, 'YYYY-MM-DD') as observation_day, " +
  "value::text as value, source, fetched_at, age_days, is_fresh from mart.macro_series_status order by series_id";

export const MONTHS_SQL =
  "select series_id, to_char(observation_date, 'YYYY-MM-DD') as observation_day, value::text as value " +
  "from mart.macro_series_current " +
  "where observation_date >= ((now() at time zone 'UTC')::date - interval '12 months')::date " +
  "order by observation_date desc, series_id";

const day = (value: unknown): string => String(value).slice(0, 10);

export async function loadMacroPanel(principal: MacroPrincipal | null): Promise<MacroOutcome> {
  return runAdminRead<MacroPanel>(
    principal,
    async () => {
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
          row.observation_day == null
            ? null
            : {
                observationDate: day(row.observation_day),
                value: timestampText(row.value),
                source: String(row.source),
                fetchedAt: timestampText(row.fetched_at),
                ageDays: Number(row.age_days),
              },
        isFresh: row.is_fresh === true,
      }));
      const months: MacroObservationRow[] = monthRows.map((row) => ({
        seriesId: String(row.series_id),
        observationDate: day(row.observation_day),
        value: timestampText(row.value),
      }));
      return { kind: "ready", data: { series, months } };
    },
    (error) =>
      isUndefinedTable(error) ? { kind: "unavailable", reason: "the macro series tables are not in this database yet" } : null,
  );
}
