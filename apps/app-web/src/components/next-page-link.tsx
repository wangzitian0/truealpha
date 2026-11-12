import Link from "next/link";

/**
 * Standardized pagination next-page link button (#1679).
 */
export function NextPageLink({
  hasMore,
  nextCursor,
  href,
}: {
  hasMore: boolean;
  nextCursor: string | null;
  href: string;
}) {
  if (!hasMore || nextCursor === null) return null;
  return (
    <Link
      href={href}
      className="inline-block rounded-lg border border-border bg-card px-4 py-2 text-sm text-gray-300 hover:border-accent"
    >
      Next page →
    </Link>
  );
}
