import { redirect } from "next/navigation";
import { loadFundValuation } from "@/server/mart/fund-valuation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function formatPct(value: string | null): string {
  return value === null ? "—" : `${Number(value).toFixed(2)}%`;
}

function formatRatio(value: string | null): string {
  return value === null ? "—" : Number(value).toFixed(2);
}

export default async function HoldingsPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fholdings");
  const funds = await loadFundValuation();

  return (
    <section aria-labelledby="holdings-heading" className="space-y-6">
      <div>
        <h1 id="holdings-heading" className="text-2xl font-bold tracking-tight">
          Fund holdings &amp; valuation
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          The fund&apos;s own filed N-PORT weights (newest vintage, captured weekly from SEC EDGAR), joined to the
          governed TOPT valuation run per listing — a join over two materialized planes, computed nowhere. Coverage is
          stated, never assumed: unresolved and unvalued weight stays visible. Tier here is the datahub&apos;s
          GPPE-band tier (mechanical thresholds over gross profit per employee); the Rankings page&apos;s tier is the
          strategy&apos;s curated theme — same word, two vocabularies, deliberately not reconciled.
        </p>
      </div>

      {funds.length === 0 ? (
        <p className="text-sm text-gray-400">
          No holdings captured yet — the weekly refresh lands the first vintage on its next run.
        </p>
      ) : (
        funds.map((fund) => (
          <div key={fund.fundId} className="space-y-3">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <h2 className="text-lg font-semibold">{fund.fundName}</h2>
              <span className="text-xs text-gray-400">
                period {fund.reportPeriod} · filed weight {fund.totalWeightPct}% · resolved {fund.resolvedWeightPct}% ·
                valued {fund.valuedWeightPct}%
                {fund.weightedGap !== null ? ` · weighted gap (valued mass) ${fund.weightedGap}` : ""}
                {fund.runId ? ` · run ${fund.runId.slice(0, 20)}…` : " · no governed valuation run yet"}
              </span>
            </div>
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-gray-400">
                    <th className="px-3 py-2">Holding</th>
                    <th className="px-3 py-2">Ticker</th>
                    <th className="px-3 py-2 text-right">Weight</th>
                    <th className="px-3 py-2 text-right">P/S</th>
                    <th className="px-3 py-2 text-right">Target mid</th>
                    <th className="px-3 py-2 text-right">Gap</th>
                    <th className="px-3 py-2">GPPE tier</th>
                  </tr>
                </thead>
                <tbody>
                  {fund.lines.map((line, index) => (
                    <tr key={`${line.holdingName}-${index}`} className="border-t border-border">
                      <td className="px-3 py-1.5">{line.holdingName}</td>
                      <td className="px-3 py-1.5 font-mono text-xs text-gray-400">{line.ticker ?? "—"}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatPct(line.weightPct)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatRatio(line.currentPs)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatRatio(line.targetPsMidpoint)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatRatio(line.valuationGap)}</td>
                      <td className="px-3 py-1.5 text-xs">{line.tier ?? "—"}</td>
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
