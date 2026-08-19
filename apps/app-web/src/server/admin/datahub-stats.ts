/**
 * Datahub statistics for the Operate world (#641 D5).
 *
 * The init.md design gives datahub three consumption surfaces and usage views;
 * what existed were two detail pages and no statistics at all — the numbers
 * that say whether the plant is healthy (governed heads, per-factor
 * availability, source activity, capture history) lived only in psql sessions.
 * This loader is the read contract for the /admin/datahub dashboard: heads
 * with their quality grades (including the #644 factor-level availability),
 * per-source fetch activity, and recent capture runs. Read-only through
 * app_ops_reader, like every loader in this directory.
 */

import { withOpsReader } from "./ops";

export interface FactorAvailabilityRow {
  factor_id: string;
  required_semantics: string[];
  complete_subjects: number;
  universe_subjects: number;
  ratio: string;
}

export interface HeadStatusRow {
  universe_id: string;
  sequence: number;
  advanced_at: string;
  target_run_id: string;
  availability: string | null;
  agreed_cells: number | null;
  total_cells: number | null;
  factors: FactorAvailabilityRow[];
}

export interface SourceStatRow {
  source: string;
  fetches_total: number;
  fetches_24h: number;
  last_fetch: string;
}

export interface CaptureRunRow {
  run_id: string;
  universe_id: string;
  cutoff: string;
  obligations: number;
  resolved: number;
  failed: number;
  complete: boolean;
}

export interface DatahubStats {
  heads: HeadStatusRow[];
  sources: SourceStatRow[];
  runs: CaptureRunRow[];
}

const HEADS_SQL = `
  select h.universe_id, h.sequence, h.advanced_at::text as advanced_at, h.target_run_id,
         q.payload->>'availability' as availability,
         (select count(*)::int from jsonb_each(q.payload->'reconciliation_cells') c
           where c.value->>'outcome' = 'agreed') as agreed_cells,
         (select count(*)::int from jsonb_each(q.payload->'reconciliation_cells')) as total_cells,
         coalesce(q.payload->'factor_availability', '{}'::jsonb) as factor_availability
  from mart.current_pointer_head h
  left join mart.datahub_quality_report q on q.run_id = h.target_run_id
  order by h.universe_id
`;

const SOURCES_SQL = `
  select source, count(*)::int as fetches_total,
         (count(*) filter (where recorded_at > now() - interval '24 hours'))::int as fetches_24h,
         max(recorded_at)::text as last_fetch
  from raw.fetches
  group by source
  order by max(recorded_at) desc, count(*) desc
  limit 12
`;

const RUNS_SQL = `
  select run_id, universe_id, cutoff::text as cutoff,
         obligation_count as obligations,
         (success_count + unchanged_count)::int as resolved,
         failed_count as failed, complete
  from mart.topt_capture_status
  order by cutoff desc
  limit 12
`;

interface FactorGrade {
  required_semantics: string[];
  complete_subjects: number;
  universe_subjects: number;
  ratio: string;
}

interface HeadDbRow extends Omit<HeadStatusRow, "factors"> {
  factor_availability: Record<string, FactorGrade>;
}

export async function loadDatahubStats(): Promise<DatahubStats> {
  return withOpsReader(async (client) => {
    const [heads, sources, runs] = await Promise.all([
      client.query<HeadDbRow>(HEADS_SQL),
      client.query<SourceStatRow>(SOURCES_SQL),
      client.query<CaptureRunRow>(RUNS_SQL),
    ]);
    return {
      heads: heads.rows.map((row: HeadDbRow) => ({
        universe_id: row.universe_id,
        sequence: row.sequence,
        advanced_at: row.advanced_at,
        target_run_id: row.target_run_id,
        availability: row.availability,
        agreed_cells: row.agreed_cells,
        total_cells: row.total_cells,
        factors: Object.entries(row.factor_availability ?? {}).map(([factorId, grade]) => ({
          factor_id: factorId,
          required_semantics: grade.required_semantics,
          complete_subjects: grade.complete_subjects,
          universe_subjects: grade.universe_subjects,
          ratio: grade.ratio,
        })),
      })),
      sources: sources.rows,
      runs: runs.rows,
    };
  });
}
