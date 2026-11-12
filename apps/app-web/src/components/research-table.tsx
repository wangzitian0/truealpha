import type { ReactNode } from "react";

/**
 * Shared research table wrapper and header row (#1679).
 * Preserves the exact DOM structure and classes of the research tables it replaces.
 */
export function ResearchTable({
  caption,
  headers,
  children,
  className = "overflow-x-auto rounded-xl border border-border",
  tableTestId,
}: {
  caption: string;
  headers: readonly string[];
  children: ReactNode;
  className?: string;
  tableTestId?: string;
}) {
  return (
    <div className={className}>
      <table className="w-full text-left text-sm" data-testid={tableTestId}>
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-card text-xs uppercase text-gray-500">
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
