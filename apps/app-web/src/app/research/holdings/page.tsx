import { redirect } from "next/navigation";
import { formatCell, formatRatio } from "@/client/format";
import { valuationBasisLabel } from "@/contracts/strategyRun";
import { loadFundValuation } from "@/server/mart/fund-valuation";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function RightTd({ raw, display }: { raw: string | null; display: string }) {
  return <td className="px-3 py-1.5 text-right tabular-nums" title={raw ?? undefined}>{display}</td>;
}

export default async function HoldingsPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fholdings");
  const funds = await loadFundValuation();

  return (
    <section aria-labelledby="holdings-heading" className="space-y-6">
      <div>
        <h1 id="holdings-heading" className="text-2xl font-bold tracking-tight">Fund holdings &amp; valuation</h1>
        <p className="mt-2 text-sm text-gray-400">
          The fund&apos;s own filed N-PORT weights (newest vintage, captured weekly from SEC EDGAR), joined to the
          governed TOPT valuation run per listing — a join over two materialized planes, computed nowhere. Coverage is
          stated, never assumed: unresolved and unvalued weight stays visible. Tier here is the datahub&apos;s
          GPPE-band tier (mechanical thresholds over gross profit per employee); the Rankings page&apos;s tier is the
          strategy&apos;s curated theme — same word, two vocabularies, deliberately not reconciled.
        </p>
      </div>

      {funds.length === 0 ? (
        <p className="text-sm text-gray-400">No holdings captured yet — the weekly refresh lands the first vintage on its next run.</p>
      ) : (
        funds.map((fund) => (
          <div key={fund.fundId} className="space-y-3">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <h2 className="text-lg font-semibold">{fund.fundName}</h2>
              <span className="text-xs text-gray-400">
                period {fund.reportPeriod} · filed weight {fund.totalWeightPct}% · resolved {fund.resolvedWeightPct}% ·
                valued {fund.valuedWeightPct}%
                {fund.weightedGap !== null
                  ? ` · weighted gap (valued mass, ${fund.weightedGapBasis !== null ? valuationBasisLabel(fund.weightedGapBasis) : "basis not recorded"}) ${fund.weightedGap}`
                  : fund.reasonCodes.length > 0
                    ? ` · weighted gap refused (${fund.reasonCodes.join(", ")})`
                    : " · no weighted gap for this run"}
                {fund.availabilityStatus !== null ? ` · ${fund.availabilityStatus}/${fund.sourceEvidenceStatus}/${fund.factorValidationStatus}` : ""}
                {fund.runId ? ` · run ${fund.runId.slice(0, 20)}…` : " · no governed valuation run yet"}
              </span>
            </div>
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-gray-400">
                    <th className="px-3 py-2">Holding</th>
                    <th className="px-3 py-2">Ticker</th>
                    {["Weight", "P/S", "P/GP", "Target mid", "Gap"].map((h) => (
                      <th key={h} className="px-3 py-2 text-right">{h}</th>
                    ))}
                    <th className="px-3 py-2">GPPE tier</th>
                  </tr>
                </thead>
                <tbody>
                  {fund.lines.map((line, index) => {
                    const weightFmt = formatRatio(line.weightPct);
                    const targetPgp = formatRatio(line.targetPgpMidpoint);
                    const targetPs = formatRatio(line.targetPsMidpoint);
                    const gapFmt = formatRatio(line.valuationGap);
                    return (
                      <tr key={`${line.holdingName}-${index}`} className="border-t border-border">
                        <td className="px-3 py-1.5">{line.holdingName}</td>
                        <td className="px-3 py-1.5 font-mono text-xs text-gray-400">{formatCell(line.ticker)}</td>
                        <RightTd raw={line.weightPct} display={weightFmt !== null ? `${weightFmt}%` : "—"} />
                        <RightTd raw={line.currentPs} display={formatCell(formatRatio(line.currentPs))} />
                        <RightTd raw={line.currentPgp} display={formatCell(formatRatio(line.currentPgp))} />
                        <RightTd
                          raw={line.targetPgpMidpoint ?? line.targetPsMidpoint}
                          display={targetPgp !== null ? `${targetPgp} P/GP` : targetPs !== null ? `${targetPs} P/S` : "—"}
                        />
                        <RightTd
                          raw={line.valuationGap}
                          display={gapFmt === null ? "—" : `${gapFmt} ${line.currentPgp !== null ? "P/GP" : "P/S"}`}
                        />
                        <td className="px-3 py-1.5 text-xs">{formatCell(line.tier)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </section>
  );
}
