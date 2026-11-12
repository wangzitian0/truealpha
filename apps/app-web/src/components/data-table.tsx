/**
 * Shared table and notice primitives for the admin and research pages (#1679).
 * Server-rendered; the markup is identical to the page-local copies they replace.
 */

import type { ReactNode } from "react";

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

/** The bordered card paragraph that a section shows when it has no rows. */
export function EmptyNotice({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-border bg-card p-4 text-gray-400">{children}</p>;
}
