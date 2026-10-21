/**
 * #1255: /admin/macro — the macro base data panel. Administrator only (the /admin layout gates it, and
 * the loader re-checks).
 *
 * Per series: the latest value and its date, the age against the cadence that the registration declares,
 * the source and the fetch time. Below the cards: the last 12 months, one row per observation date.
 * The page formats what the mart serves. It computes no age and no verdict: both come from
 * `mart.macro_series_status`, the view that the nightly verdict `macro_freshness` reads too.
 */

import { formatCell, formatUtcMinute, formatYieldPercent } from "@/client/format";
import { ReadStateNotice } from "@/components/read-state";
import { loadMacroPanel, type MacroSeries } from "@/server/admin/macro";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function ageWords(series: MacroSeries): string {
  if (series.latest === null) return "no observation yet";
  const unit = series.latest.ageDays === 1 ? "day" : "days";
  return `${series.latest.ageDays} ${unit} old, limit ${series.maxAgeDays}`;
}

export default async function MacroPage() {
  const principal = await getServerPrincipal();
  const outcome = await loadMacroPanel(principal === null ? null : { principalKind: principal.principalKind });

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="macro-heading" className="space-y-4">
        <h1 id="macro-heading" className="text-2xl font-bold tracking-tight">
          Macro base data
        </h1>
        <ReadStateNotice
          state={outcome}
          overrides={{
            denied: "Access denied. This area requires a verified administrator identity.",
            empty: "No macro series is registered in this database yet. The macro job has not run.",
          }}
        />
      </section>
    );
  }

  const { series, months } = outcome.data;
  const dates = Array.from(new Set(months.map((row) => row.observationDate)));
  const valueOf = new Map(months.map((row) => [`${row.observationDate}|${row.seriesId}`, row.value]));

  return (
    <section aria-labelledby="macro-heading" className="space-y-8">
      <div>
        <h1 id="macro-heading" className="text-2xl font-bold tracking-tight">
          Macro base data
        </h1>
        <p className="mt-2 text-gray-400">
          The U.S. Treasury par yield curve, loaded every day by the macro job and read through the{" "}
          <code className="text-accent">mart</code> read-only role. The 3-month yield is the risk-free rate of the core
          strategy (#1255).
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3" data-macro-series="cards">
        {series.map((item) => (
          <article
            key={item.seriesId}
            data-series-id={item.seriesId}
            data-fresh={item.isFresh ? "true" : "false"}
            className="rounded-xl border border-border bg-card p-5"
          >
            <h2 className="font-semibold">{item.label}</h2>
            <p className="mt-1 text-xs text-gray-500">
              <code>{item.seriesId}</code>
            </p>
            <p
              className="mt-3 text-3xl font-bold tracking-tight"
              title={item.latest === null ? undefined : item.latest.value}
            >
              {formatCell(item.latest === null ? null : formatYieldPercent(item.latest.value))}
            </p>
            <p className="mt-1 text-sm text-gray-400">
              {item.latest === null ? "no observation" : `observed ${item.latest.observationDate}`}
            </p>
            <p
              data-freshness={item.isFresh ? "fresh" : "stale"}
              className={`mt-3 inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${
                item.isFresh ? "border-emerald-500/40 text-emerald-300" : "border-red-500/40 text-red-300"
              }`}
            >
              {item.isFresh ? "fresh" : "stale"} · {ageWords(item)}
            </p>
            <dl className="mt-3 space-y-1 text-xs text-gray-400">
              <div className="flex gap-2">
                <dt className="text-gray-500">Source</dt>
                <dd>{formatCell(item.latest?.source ?? null)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-gray-500">Fetched</dt>
                <dd title={item.latest?.fetchedAt}>{formatCell(formatUtcMinute(item.latest?.fetchedAt ?? null))}</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>

      <div className="rounded-xl border border-border bg-card p-5">
        <h2 className="font-semibold">Last 12 months</h2>
        {dates.length === 0 ? (
          <p className="mt-3 text-sm text-gray-400">No observation in the last 12 months.</p>
        ) : (
          <div className="mt-3 max-h-[32rem] overflow-auto">
            <table className="w-full text-left text-sm" data-macro-series="months">
              <thead className="sticky top-0 bg-card text-xs uppercase tracking-wider text-gray-500">
                <tr>
                  <th className="py-2 pr-4">Date</th>
                  {series.map((item) => (
                    <th key={item.seriesId} className="py-2 pr-4 text-right">
                      {item.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {dates.map((date) => (
                  <tr key={date} className="border-t border-border">
                    <td className="py-1 pr-4 text-gray-300">{date}</td>
                    {series.map((item) => {
                      const value = valueOf.get(`${date}|${item.seriesId}`) ?? null;
                      return (
                        <td key={item.seriesId} className="py-1 pr-4 text-right tabular-nums" title={value ?? undefined}>
                          {formatCell(formatYieldPercent(value))}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
