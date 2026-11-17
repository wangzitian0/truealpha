import type { ReactNode } from "react";

export const BADGE_TONES = {
  ok: "bg-emerald-950/60 text-emerald-400 border border-emerald-800",
  warn: "bg-amber-950/60 text-amber-400 border border-amber-800",
  crit: "bg-rose-950/80 text-rose-400 border border-rose-800",
  muted: "bg-zinc-800 text-zinc-300 border border-zinc-700",
} as const;

export type BadgeTone = keyof typeof BADGE_TONES;

export function Badge({
  tone,
  className = "",
  title,
  children,
}: {
  tone?: BadgeTone;
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  const cls = ["inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium", tone ? BADGE_TONES[tone] : "", className]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/** A table with one header row of plain labels; the caller renders the body rows. */
export function DataTable({
  headers,
  children,
  className = "w-full text-left text-sm",
}: {
  headers: readonly string[];
  children: ReactNode;
  className?: string;
}) {
  return (
    <table className={className}>
      <thead>
        <tr className="text-gray-500">
          {headers.map((h, i) => (
            <th key={h} className={i < headers.length - 1 ? "pr-4 font-normal" : "font-normal"}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

export type TableCell = ReactNode | { content: ReactNode; className?: string; title?: string };

function isCellObj(c: TableCell): c is { content: ReactNode; className?: string; title?: string } {
  return typeof c === "object" && c !== null && "content" in c && !("$$typeof" in c);
}

export function MappedTable<T>({
  headers,
  rows,
  rowKey,
  cells,
  colClasses,
  rowClassName = () => "border-t border-border",
  rowData,
  className,
}: {
  headers: readonly string[];
  rows: readonly T[];
  rowKey: (row: T) => string | number;
  cells: (row: T) => TableCell[];
  colClasses?: readonly string[];
  rowClassName?: (row: T) => string;
  rowData?: (row: T) => Record<string, string | undefined>;
  className?: string;
}) {
  return (
    <DataTable headers={headers} className={className}>
      {rows.map((row) => (
        <tr key={rowKey(row)} className={rowClassName(row)} {...rowData?.(row)}>
          {cells(row).map((cell, idx, arr) => {
            const { content, className: cellCls = "", title } = isCellObj(cell) ? cell : { content: cell };
            const pr = idx < arr.length - 1 ? "pr-4" : "";
            const cls = [pr, colClasses?.[idx] ?? "", cellCls].filter(Boolean).join(" ");
            return (
              <td key={idx} className={cls || undefined} title={title}>
                {content}
              </td>
            );
          })}
        </tr>
      ))}
    </DataTable>
  );
}

export function Section({
  id,
  title,
  badge,
  subtitle,
  emptyText,
  isEmpty,
  children,
}: {
  id?: string;
  title: ReactNode;
  badge?: ReactNode;
  subtitle?: ReactNode;
  emptyText?: ReactNode;
  isEmpty?: boolean;
  children: ReactNode;
}) {
  const heading = <h2 className="text-lg font-semibold">{title}</h2>;
  return (
    <div id={id} className="space-y-3">
      {subtitle || badge ? (
        <div>
          {badge ? <div className="flex items-center gap-3">{heading}{badge}</div> : heading}
          {subtitle && <p className="text-sm text-gray-500">{subtitle}</p>}
        </div>
      ) : (
        heading
      )}
      {isEmpty && emptyText ? <EmptyNotice>{emptyText}</EmptyNotice> : children}
    </div>
  );
}

export function MetricCard({
  title,
  badge,
  tone,
  metric,
  unit,
  foot,
}: {
  title: string;
  badge: ReactNode;
  tone: BadgeTone;
  metric: ReactNode;
  unit?: string;
  foot?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between text-xs text-gray-400">
          <span className="uppercase tracking-wider font-semibold">{title}</span>
          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${BADGE_TONES[tone]}`}>
            {badge}
          </span>
        </div>
        <div className="mt-3 text-2xl font-bold tracking-tight">
          {metric}
          {unit && <span className="ml-1 text-sm font-normal text-gray-400">{unit}</span>}
        </div>
      </div>
      {foot && <p className="mt-2 text-xs text-gray-400">{foot}</p>}
    </div>
  );
}

export function SummaryTile({
  label,
  value,
  badge,
  valueClass = "tabular-nums",
  valueTitle,
  valueData,
  foot,
  footClass = "text-gray-500",
  footTitle,
}: {
  label: string;
  value: ReactNode;
  badge?: ReactNode;
  valueClass?: string;
  valueTitle?: string;
  valueData?: Record<string, string | undefined>;
  foot?: ReactNode;
  footClass?: string;
  footTitle?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      {badge ? (
        <div className="flex items-center justify-between gap-2">
          <dt className="text-xs uppercase tracking-wide text-gray-500">{label}</dt>
          {badge}
        </div>
      ) : (
        <dt className="text-xs uppercase tracking-wide text-gray-500">{label}</dt>
      )}
      <dd className={`mt-1 text-lg font-semibold ${valueClass}`.trim()} title={valueTitle} {...valueData}>
        {value}
      </dd>
      {foot !== undefined && (
        <p className={`mt-1 text-xs ${footClass}`} title={footTitle}>
          {foot}
        </p>
      )}
    </div>
  );
}

/** The bordered card paragraph that a section shows when it has no rows. */
export function EmptyNotice({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-border bg-card p-4 text-gray-400">{children}</p>;
}
