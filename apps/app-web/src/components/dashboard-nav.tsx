/** Primary dashboard navigation — see #370. */

import Link from "next/link";

const LINKS: readonly { href: string; label: string }[] = [
  { href: "/", label: "Overview" },
  { href: "/rankings", label: "Rankings / themes" },
  { href: "/compare", label: "Comparison" },
  { href: "/admin/strategy-runs", label: "Strategy runs" },
];

export function DashboardNav() {
  return (
    <nav aria-label="Dashboard sections" className="flex flex-wrap gap-2">
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-gray-300 hover:border-accent hover:text-white"
        >
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
