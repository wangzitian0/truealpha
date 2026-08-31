import { redirect } from "next/navigation";
import { loadFundHoldings } from "@/server/mart/fund-holdings";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function formatPct(value: string | null): string {
  return value === null ? "—" : `${Number(value).toFixed(2)}%`;
}

function formatUsd(value: string | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(
    Number(value),
  );
}

export default async function HoldingsPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fholdings");
  const funds = await loadFundHoldings();

  return (
    <section aria-labelledby="holdings-heading" className="space-y-6">
      <div>
        <h1 id="holdings-heading" className="text-2xl font-bold tracking-tight">
          Fund holdings
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Each fund&apos;s own filed N-PORT weights, newest vintage per fund — captured weekly from SEC EDGAR with the
          filing date as the knowable moment. Weights are the fund&apos;s assertion, not a computation.
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
                period {fund.reportPeriod} · filed {fund.filedOn} · {fund.lines.length} equity lines · weights sum{" "}
                {fund.weightSumPct}%
              </span>
            </div>
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-gray-400">
                    <th className="px-3 py-2">Holding</th>
                    <th className="px-3 py-2">ISIN</th>
                    <th className="px-3 py-2 text-right">Weight</th>
                    <th className="px-3 py-2 text-right">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {fund.lines.map((line) => (
                    <tr key={`${line.isin ?? line.holdingName}`} className="border-t border-border">
                      <td className="px-3 py-1.5">{line.holdingName}</td>
                      <td className="px-3 py-1.5 font-mono text-xs text-gray-400">{line.isin ?? "—"}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatPct(line.weightPct)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{formatUsd(line.valueUsd)}</td>
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
