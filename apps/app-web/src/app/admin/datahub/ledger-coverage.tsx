import { formatCell, formatPercentFromFraction } from "@/client/format";
import { DataTable, EmptyNotice } from "@/components/data-table";

export interface LedgerCoverageRow {
  day: string;
  source: string;
  endpoint: string;
  calls: number;
  no_key: number;
  by_reason?: Record<string, number>;
  keys_twice?: number;
  run_share: string | null;
  calls_per_run: string | null;
}

export const LEDGER_COVERAGE_DAYS = 8;

const HEADERS = ["day (UTC)", "source", "endpoint", "calls", "no key", "repeat reasons", "keys twice", "with run", "calls per run"] as const;

const reasonText = (byReason: Record<string, number> = {}): string =>
  Object.entries(byReason)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([r, n]) => `${r} ${n}`)
    .join(", ");

export function LedgerCoverageTable({ rows }: { rows: LedgerCoverageRow[] }) {
  if (rows.length === 0) {
    return <EmptyNotice>No external calls recorded in the last {LEDGER_COVERAGE_DAYS} UTC days.</EmptyNotice>;
  }
  return (
    <div className="overflow-x-auto">
      <DataTable headers={HEADERS}>
        {rows.map((row) => (
          <tr key={`${row.day}|${row.source}|${row.endpoint}`} className="border-t border-border" data-ledger-coverage-row={`${row.day} ${row.source} ${row.endpoint}`}>
            <td className="pr-4 text-gray-400">{row.day}</td>
            <td className="pr-4 font-mono">{row.source}</td>
            <td className="pr-4 font-mono">{row.endpoint}</td>
            <td className="pr-4">{row.calls}</td>
            <td className={`pr-4 ${row.no_key > 0 ? "text-amber-400" : ""}`}>{row.no_key}</td>
            <td className="pr-4 font-mono text-xs text-gray-300">{formatCell(reasonText(row.by_reason))}</td>
            <td className={`pr-4 ${(row.keys_twice ?? 0) > 0 ? "text-amber-400" : ""}`}>{row.keys_twice ?? 0}</td>
            <td className="pr-4">{formatCell(formatPercentFromFraction(row.run_share))}</td>
            <td>{formatCell(row.calls_per_run)}</td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}
