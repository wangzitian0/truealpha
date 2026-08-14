"use client";

/** Normal-user route-group navigation — see #371 (was DashboardNav, #370).
 *
 * Deliberately does not link /admin: the research and administrator route
 * groups have separate server gates, and the way between them is the world
 * switch in the top bar (`components/app-chrome.tsx`), which only renders for
 * an administrator. This component must never import an administrator
 * loader/repository — tests/route-group-boundary.test.ts proves that.
 *
 * A client component only so it can read the current path: #371's finding was
 * that seven identical pills told you nothing about where you were, by sight or
 * by screen reader. `aria-current="page"` is the accessible half and the styling
 * is the visual half; both come from the same comparison. No authorization
 * state crosses to the client — the link list is static and public. */

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS: readonly { href: string; label: string }[] = [
  { href: "/research", label: "Overview" },
  { href: "/research/rankings", label: "Rankings / themes" },
  { href: "/research/compare", label: "Comparison" },
  { href: "/research/strategy", label: "Strategy" },
  { href: "/research/coverage", label: "Coverage" },
  { href: "/research/conversations", label: "Conversations" },
  { href: "/research/library", label: "Library" },
];

export function ResearchNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Research sections" className="flex flex-wrap gap-2">
      {LINKS.map((link) => {
        // Exact match only. `/research` is a prefix of every other entry, so
        // prefix matching would mark the overview current on all seven.
        const current = pathname === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={current ? "page" : undefined}
            className={
              current
                ? "rounded-lg border border-accent bg-card px-3 py-1.5 text-sm font-medium text-white"
                : "rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-gray-300 hover:border-accent hover:text-white"
            }
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
