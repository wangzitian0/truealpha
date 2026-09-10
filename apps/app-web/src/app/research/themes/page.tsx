import { redirect } from "next/navigation";
import Link from "next/link";
import { getServerPrincipal } from "@/server/auth/request-context";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { classifiedShare, loadThemePurity } from "@/server/mart/theme-purity";

export const dynamic = "force-dynamic";

function pct(value: string | null): string {
  return value === null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

/** Absolute currency as the plane holds it (63,887,000,000), printed the way a filing does. */
function millions(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return `${(amount / 1_000_000).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export default async function ThemesPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fthemes");
  const groups = await loadThemePurity();
  const names = await loadEntityDisplayMap();

  return (
    <section aria-labelledby="themes-heading" className="space-y-8">
      <div>
        <h1 id="themes-heading" className="text-2xl font-bold tracking-tight">
          Theme purity
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          What share of each issuer&apos;s revenue a theme accounts for (init.md §0 question 6), read from
          <code className="mx-1 rounded bg-card px-1">mart.issuer_theme_purity</code> and computed nowhere here. The
          denominator is the issuer&apos;s <strong>consolidated</strong> revenue, never the sum of the segments that
          happened to be classified — a missed segment would otherwise raise every remaining share and rank the
          issuer with the worst data as the purest name.
        </p>
        <p className="mt-2 text-sm text-gray-400">
          <strong>Classified</strong> is how much of that denominator carries a judgement either way. A 60% share with
          40% unclassified is a different claim from a 60% share with none, so both are shown; a row whose classified
          mass fell below the theme&apos;s floor is <strong>refused</strong> rather than ranked, and stays visible so
          the list does not look more complete than it is.
        </p>
      </div>

      {groups.length === 0 ? (
        <p className="text-sm text-gray-400">
          No theme purity materialized yet — the weekly standards lane writes the first rows after it lands a segment
          partition for a governed run.
        </p>
      ) : (
        groups.map((group) => (
          <div key={group.themeId} className="space-y-3">
            <div>
              <h2 className="text-lg font-semibold">{group.theme}</h2>
              <p className="text-xs text-gray-500">
                Definition {group.definitionVersion} ({group.definitionSha256.slice(0, 12)}) · run {group.runId} ·
                cutoff {group.cutoff}
              </p>
            </div>
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">
                  Issuers ranked by {group.theme} revenue share at {group.cutoff}
                </caption>
                <thead className="bg-card text-xs uppercase text-gray-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">Rank</th>
                    <th scope="col" className="px-4 py-3">Issuer</th>
                    <th scope="col" className="px-4 py-3">Theme share</th>
                    <th scope="col" className="px-4 py-3">Classified</th>
                    <th scope="col" className="px-4 py-3">In theme (m)</th>
                    <th scope="col" className="px-4 py-3">Consolidated (m)</th>
                    <th scope="col" className="px-4 py-3">Segments</th>
                    <th scope="col" className="px-4 py-3">Period</th>
                    <th scope="col" className="px-4 py-3">Confidence</th>
                    <th scope="col" className="px-4 py-3">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {group.rows.map((row, index) => (
                    <tr key={row.issuerId} className="border-t border-border">
                      <td className="px-4 py-3">{row.themeShare === null ? "—" : index + 1}</td>
                      <th scope="row" className="px-4 py-3 font-medium">
                        <Link
                          href={`/research/entities/${encodeURIComponent(row.issuerId)}`}
                          className="text-accent hover:underline"
                          title={row.issuerId}
                        >
                          {entityLabel(row.issuerId, names)}
                        </Link>
                      </th>
                      <td className="px-4 py-3 tabular-nums" title={row.themeShare ?? undefined}>
                        {row.themeShare === null ? (
                          <span className="text-gray-500">refused</span>
                        ) : (
                          pct(row.themeShare)
                        )}
                      </td>
                      <td className="px-4 py-3 tabular-nums">{pct(classifiedShare(row))}</td>
                      <td className="px-4 py-3 tabular-nums">{millions(row.inThemeRevenue)}</td>
                      <td className="px-4 py-3 tabular-nums">{millions(row.consolidatedRevenue)}</td>
                      <td className="px-4 py-3 tabular-nums">{row.segments}</td>
                      <td className="px-4 py-3">{row.periodEnd}</td>
                      <td className="px-4 py-3 tabular-nums">{Number(row.confidence).toFixed(2)}</td>
                      <td className="px-4 py-3 text-xs">
                        <span title={row.extractor}>{row.availabilityStatus}</span>
                        {row.reasonCodes.length > 0 && (
                          <span className="ml-1 text-gray-500">({row.reasonCodes.join(", ")})</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </section>
  );
}
