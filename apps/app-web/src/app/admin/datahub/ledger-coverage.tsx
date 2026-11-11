/**
 * #1521 step 6: the key and run coverage of the call ledger, one row per UTC day, vendor and endpoint.
 *
 * The table replaces the nightly verdict `fetch_keys_once`. It judges nothing: an amber cell marks a count above
 * zero, and no run turns red on it. SQL computes every count and ratio (`LEDGER_COVERAGE_SQL`); this view formats.
 */

import { formatCell, formatPercentFromFraction } from "@/client/format";
import { DataTable, EmptyNotice } from "@/components/data-table";
import { LEDGER_COVERAGE_DAYS, type LedgerCoverageRow } from "@/server/admin/datahub-stats";

const HEADERS = [
  "day (UTC)",
  "source",
  "endpoint",
  "calls",
  "no key",
  "repeat reasons",
  "keys twice",
  "with run",
  "calls per run",
] as const;

/** `probe 2, answer_refused 1`: the reasons by name, so a new reason shows without a code change. */
function reasonText(byReason: Record<string, number>): string {
  return Object.entries(byReason)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([reason, rows]) => `${reason} ${rows}`)
    .join(", ");
}

export function LedgerCoverageTable({ rows }: { rows: LedgerCoverageRow[] }) {
  if (rows.length === 0) {
    return (
      <EmptyNotice>
        No external calls recorded in the last {LEDGER_COVERAGE_DAYS} UTC days.
      </EmptyNotice>
    );
  }
  return (
    <div className="overflow-x-auto">
      <DataTable headers={HEADERS}>
        {rows.map((row) => (
          <tr
            key={`${row.day}|${row.source}|${row.endpoint}`}
            className="border-t border-border"
            data-ledger-coverage-row={`${row.day} ${row.source} ${row.endpoint}`}
          >
            <td className="pr-4 text-gray-400">{row.day}</td>
            <td className="pr-4 font-mono">{row.source}</td>
            <td className="pr-4 font-mono">{row.endpoint}</td>
            <td className="pr-4">{row.calls}</td>
            <td className={`pr-4 ${row.no_key > 0 ? "text-amber-400" : ""}`}>{row.no_key}</td>
            <td className="pr-4 font-mono text-xs text-gray-300">{formatCell(reasonText(row.by_reason))}</td>
            <td className={`pr-4 ${row.keys_twice > 0 ? "text-amber-400" : ""}`}>{row.keys_twice}</td>
            <td className="pr-4">{formatCell(formatPercentFromFraction(row.run_share))}</td>
            <td>{formatCell(row.calls_per_run)}</td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}
