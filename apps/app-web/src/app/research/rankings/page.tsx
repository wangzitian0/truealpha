import Link from "next/link";
import { redirect } from "next/navigation";
import { ProvenanceCell } from "@/components/provenance-cell";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadRanking } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { formatCell, formatRatio, formatSignedRatio, signColor } from "@/client/format";
import { getServerPrincipal } from "@/server/auth/request-context";
import { CardExportButton } from "@/components/card-export-button";
import {
  normalizePegConvention,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  type PegGrowthConvention,
  valuationBasisLabel,
} from "@/contracts/strategyRun";

export const dynamic = "force-dynamic";

function buildRankingsHref(params: {
  cursor?: string | null;
  cutoff?: string;
  convention?: PegGrowthConvention;
  universe?: string;
}): string {
  const query = new URLSearchParams();
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.cutoff) query.set("cutoff", params.cutoff);
  if (params.convention && params.convention !== "historical_cagr") {
    query.set("convention", params.convention);
  }
  if (params.universe && params.universe !== "topt") {
    query.set("universe", params.universe);
  }
  const qs = query.toString();
  return qs.length > 0 ? `/research/rankings?${qs}` : "/research/rankings";
}

export default async function RankingsPage({
  searchParams,
}: {
  searchParams: Promise<{ cursor?: string; cutoff?: string; convention?: string; universe?: string }>;
}) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Frankings");
  const params = await searchParams;
  const convention = normalizePegConvention(params.convention);
  const selectedUniverse = params.universe === "qqq" ? "qqq" : "topt";
  const state = await loadRanking(principal.context, { cutoffAt: params.cutoff, cursor: params.cursor ?? null });
  const names = await loadEntityDisplayMap();
  // #1235: one run, one definition, one basis. Under definition 1.0.0 the gap divides P/GP.
  const basisLabel = valuationBasisLabel(
    state.kind === "ready"
      ? (state.data.rows.find((row) => row.valuationBasis !== null)?.valuationBasis ?? null)
      : null,
  );

  return (
    <section aria-labelledby="rankings-heading" className="space-y-6">
      <div>
        <h1 id="rankings-heading" className="text-2xl font-bold tracking-tight">
          Valuation rankings
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          The strategy&apos;s large-model-value universe ranked by valuation gap, read from the materialized strategy
          run through the mart read adapter (#370), not recomputed here.
        </p>
        <p className="mt-2 text-sm text-gray-400">
          Titled &ldquo;Theme rankings&mdash;purest large-model-value names&rdquo; until #772. It ranks a valuation
          gap, and &ldquo;purest&rdquo; is init.md question 6&apos;s word for a revenue share, which now has its own
          page:{" "}
          <Link href="/research/themes" className="text-accent hover:underline">
            Theme purity
          </Link>
          . Same word, two measurements &mdash; and the one this page shows is the valuation.
        </p>
      </div>

      <ReadStateNotice state={state} />

      <ClaimCeilingBanner />

      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2" data-testid="peg-convention-selector">
          <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
            Growth-Rate Convention (Module 1):
          </span>
          {PEG_GROWTH_CONVENTIONS.map((item) => {
            const active = item === convention;
            return (
              <Link
                key={item}
                href={buildRankingsHref({
                  cutoff: params.cutoff,
                  convention: item,
                  universe: selectedUniverse,
                })}
                data-peg-convention={item}
                aria-current={active ? "page" : undefined}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                  active
                    ? "border border-sky-500 bg-sky-950/70 text-sky-300"
                    : "border border-border bg-slate-900 text-gray-400 hover:border-gray-600 hover:text-gray-200"
                }`}
              >
                {PEG_CONVENTION_LABELS[item]} ({item})
              </Link>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center gap-2" data-testid="universe-selector">
          <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
            Universe Scope:
          </span>
          {(["topt", "qqq"] as const).map((univ) => {
            const active = univ === selectedUniverse;
            return (
              <Link
                key={univ}
                href={buildRankingsHref({
                  cutoff: params.cutoff,
                  convention,
                  universe: univ,
                })}
                data-universe-selector={univ}
                aria-current={active ? "page" : undefined}
                className={`rounded-md px-2.5 py-1 font-mono text-xs font-medium uppercase transition ${
                  active
                    ? "border border-sky-500 bg-sky-950/70 text-sky-300"
                    : "border border-border bg-slate-900 text-gray-400 hover:border-gray-600 hover:text-gray-200"
                }`}
              >
                {univ === "qqq" ? "QQQ (Nasdaq-100)" : "TOPT (Top-100)"}
              </Link>
            );
          })}
        </div>
      </div>

      {state.kind === "ready" && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="text-sm text-gray-500">
              Cutoff {state.data.cutoffAt} — {state.data.page.total} member(s) · Convention:{" "}
              <span className="font-mono text-gray-300">{convention}</span> · Universe:{" "}
              <span className="font-mono uppercase text-gray-300">{selectedUniverse}</span>
            </p>
            <CardExportButton cutoff={params.cutoff} />
          </div>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Ranked issuers for the large-model-value theme at {state.data.cutoffAt}</caption>
              <thead className="bg-card text-xs uppercase text-gray-500">
                <tr>
                  <th scope="col" className="px-4 py-3">Rank</th>
                  <th scope="col" className="px-4 py-3">Issuer</th>
                  <th scope="col" className="px-4 py-3">Tier</th>
                  <th scope="col" className="px-4 py-3">Current P/S</th>
                  <th scope="col" className="px-4 py-3">Current {basisLabel}</th>
                  <th scope="col" className="px-4 py-3">Target {basisLabel}</th>
                  <th scope="col" className="px-4 py-3">Valuation gap ({basisLabel})</th>
                  {/* Module 1 (#284). Headed "PEG rank" rather than "Rank" because it is a
                      SEPARATE ordering: PEG does not participate in selection, and showing
                      the two under one heading would imply it does. */}
                  <th scope="col" className="px-4 py-3">PEG ({PEG_CONVENTION_LABELS[convention]})</th>
                  <th scope="col" className="px-4 py-3">PEG rank</th>
                  <th scope="col" className="px-4 py-3">Confidence</th>
                  <th scope="col" className="px-4 py-3">Availability</th>
                  <th scope="col" className="px-4 py-3">Inputs</th>
                </tr>
              </thead>
              <tbody>
                {state.data.rows.map((row) => {
                  const rowPeg = convention === "historical_cagr" ? row.peg : null;
                  const rowPegRank = convention === "historical_cagr" ? row.pegRank : null;
                  const pegReasons =
                    convention === "historical_cagr"
                      ? (row.pegReasonCodes ?? [])
                      : [`growth_convention_unsourced:${convention}`];
                  return (
                    <tr key={row.issuerId} className="border-t border-border">
                      <td className="px-4 py-3">{row.rank ?? "—"}</td>
                      <th scope="row" className="px-4 py-3 font-medium">
                        <Link
                          href={`/research/entities/${encodeURIComponent(row.issuerId)}${
                            convention !== "historical_cagr" ? `?convention=${encodeURIComponent(convention)}` : ""
                          }`}
                          className="text-accent hover:underline"
                          title={row.issuerId}
                        >
                          {entityLabel(row.issuerId, names)}
                        </Link>
                      </th>
                      <td className="px-4 py-3">{formatCell(row.tier)}</td>
                      <td className="px-4 py-3 tabular-nums" title={row.currentPriceToSales ?? undefined}>
                        {formatCell(formatRatio(row.currentPriceToSales))}
                      </td>
                      <td className="px-4 py-3 tabular-nums" title={row.currentMultiple ?? undefined}>
                        {formatCell(formatRatio(row.currentMultiple))}
                      </td>
                      <td className="px-4 py-3 tabular-nums" title={row.targetMultiple ?? undefined}>
                        {formatCell(formatRatio(row.targetMultiple))}
                      </td>
                      <td
                        className={`px-4 py-3 tabular-nums ${signColor(row.valuationGap)}`}
                        title={row.valuationGap ?? undefined}
                      >
                        {formatCell(formatSignedRatio(row.valuationGap))}
                      </td>
                      <td className="px-4 py-3 tabular-nums" title={rowPeg ?? undefined}>
                        <div>{formatCell(formatRatio(rowPeg))}</div>
                        {rowPeg === null && pegReasons.length > 0 && (
                          <div className="mt-0.5 font-mono text-[11px] text-amber-400" data-testid="peg-reason-codes">
                            {pegReasons.join(", ")}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 tabular-nums">{formatCell(rowPegRank?.toString() ?? null)}</td>
                      <td className="px-4 py-3 tabular-nums" title={row.confidence ?? undefined}>
                        {formatCell(formatRatio(row.confidence))}
                      </td>
                      <td className="px-4 py-3">
                        <AvailabilityBadge status={row.availability} />
                      </td>
                      <td className="px-4 py-3">
                        <ProvenanceCell row={row} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {state.data.page.hasMore && state.data.page.nextCursor !== null && (
            <Link
              href={buildRankingsHref({
                cursor: state.data.page.nextCursor,
                cutoff: params.cutoff,
                convention,
                universe: selectedUniverse,
              })}
              className="inline-block rounded-lg border border-border bg-card px-4 py-2 text-sm text-gray-300 hover:border-accent"
            >
              Next page →
            </Link>
          )}
        </>
      )}
    </section>
  );
}
