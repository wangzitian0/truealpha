"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS: readonly { href: string; label: string }[] = [
  { href: "/research/coverage", label: "Coverage" },
  { href: "/research/rankings", label: "Rankings" },
  { href: "/research/themes", label: "Theme purity" },
  { href: "/research/holdings", label: "Holdings" },
  { href: "/research/library", label: "Library" },
  { href: "/research/backtest", label: "Backtests" },
];

export function ResearchNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Research sections" className="flex flex-wrap gap-2">
      {LINKS.map((link) => {
        const current = pathname === link.href || pathname.startsWith(`${link.href}/`);
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
