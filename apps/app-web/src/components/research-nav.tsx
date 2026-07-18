/** Normal-user route-group navigation — see #371 (was DashboardNav, #370).
 * Deliberately does not link /admin: the research and administrator route
 * groups have separate server gates and separate navs; this component must
 * never import an administrator loader/repository. */

import Link from "next/link";

const LINKS: readonly { href: string; label: string }[] = [
  { href: "/research", label: "Overview" },
  { href: "/research/rankings", label: "Rankings / themes" },
  { href: "/research/compare", label: "Comparison" },
  { href: "/research/conversations", label: "Conversations" },
  { href: "/research/library", label: "Library" },
];

export function ResearchNav() {
  return (
    <nav aria-label="Research sections" className="flex flex-wrap gap-2">
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
