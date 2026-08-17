/**
 * #539: universe provenance — which universes the pipelines capture, published
 * from which governed version, with every constituent traceable to the exact
 * bytes the index operator answered.
 */

import { loadUniverses } from "@/server/admin/universes";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function shortSha(sha: string): string {
  return sha ? `${sha.slice(0, 12)}…` : "—";
}

export default async function AdminUniversesPage() {
  const principal = await getServerPrincipal();
  if (!principal || principal.principalKind !== "administrator") {
    return <p className="text-amber-400">Access denied.</p>;
  }
  const overview = await loadUniverses();

  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Universes</h1>
      <p className="text-sm text-gray-500">
        Governed universe lists, published from the constituent data plane. Every row traces to the index
        operator&apos;s verbatim response bytes; membership refreshes publish a new version only when the
        mapping actually changed. The TOPT&nbsp;20 remains frozen corpus configuration until migrated here.
      </p>

      {overview.heads.length === 0 && (
        <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
          No governed universe heads published yet — run the constituent refresh with --publish.
        </p>
      )}

      {overview.heads.map((head) => (
        <div key={head.kind} className="space-y-3">
          <h2 className="font-mono text-lg text-accent">{head.universe_id}</h2>
          <p className="text-xs text-gray-500">
            {head.kind} · sequence {head.sequence} · {head.instrument_count} instruments · mapping{" "}
            <span className="font-mono">{shortSha(head.mapping_sha256)}</span> · published {head.advanced_at} ·{" "}
            {head.note}
          </p>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wider text-gray-500">
                <tr>
                  <th className="px-3 py-2">Ticker</th>
                  <th className="px-3 py-2">Company</th>
                  <th className="px-3 py-2">CIK</th>
                  <th className="px-3 py-2">FIGI</th>
                  <th className="px-3 py-2">Market cap</th>
                  <th className="px-3 py-2">As of</th>
                  <th className="px-3 py-2">Source bytes</th>
                </tr>
              </thead>
              <tbody>
                {(overview.members[head.kind] ?? []).map((member) => (
                  <tr key={member.ticker} className="border-t border-border/60">
                    <td className="px-3 py-1.5 font-mono">{member.ticker}</td>
                    <td className="px-3 py-1.5">{member.company_name}</td>
                    <td className="px-3 py-1.5 font-mono">{member.cik ?? "—"}</td>
                    <td className="px-3 py-1.5 font-mono text-xs">{member.figi ?? "—"}</td>
                    <td className="px-3 py-1.5 text-right">{member.market_cap ?? "—"}</td>
                    <td className="px-3 py-1.5">{member.as_of}</td>
                    <td className="px-3 py-1.5 font-mono text-xs" title={member.object_uri ?? ""}>
                      {member.source} · {shortSha(member.fetch_sha256)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {overview.history.length > overview.heads.length && (
        <div>
          <h2 className="text-lg font-semibold">Version history</h2>
          <ul className="mt-2 space-y-1 text-xs text-gray-500">
            {overview.history.map((row) => (
              <li key={`${row.kind}:${row.sequence}`} className="font-mono">
                {row.kind} seq {row.sequence} · {row.universe_id} · {shortSha(row.mapping_sha256)} ·{" "}
                {row.advanced_at} · {row.note}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
