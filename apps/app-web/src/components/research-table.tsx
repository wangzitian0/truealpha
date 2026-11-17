import type { ReactNode } from "react";

/** Shared research table wrapper and header row (#1679). */
export function ResearchTable({
  caption,
  headers,
  children,
  className = "overflow-x-auto rounded-xl border border-border",
  tableClassName = "w-full text-left text-sm",
  theadClassName = "bg-card text-xs uppercase text-gray-500",
  tableTestId,
}: {
  caption?: string;
  headers: readonly string[];
  children: ReactNode;
  className?: string;
  tableClassName?: string;
  theadClassName?: string;
  tableTestId?: string;
}) {
  return (
    <div className={className}>
      <table className={tableClassName} data-testid={tableTestId}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className={theadClassName}>
          <tr>
            {headers.map((h) => (
              <th key={h} scope="col" className="px-4 py-3">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
