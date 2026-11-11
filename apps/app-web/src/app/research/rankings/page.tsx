import Link from "next/link";
import { redirect } from "next/navigation";
import { ProvenanceCell } from "@/components/provenance-cell";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadRanking } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import {
  formatCell,
  formatNumberMagnitude,
  formatRatio,
  formatSignedRatio,
  formatTierLabel,
  formatUsdMagnitude,
  signColor,
} from "@/client/format";
import {
  GPPE_FACTOR_VARIANT_KEYS,
  RANKING_PANEL_INPUT_KEYS,
  type WidePanelCell,
} from "@/server/mart/research-read";
import { getServerPrincipal } from "@/server/auth/request-context";
import { CardExportButton } from "@/components/card-export-button";
import {
  normalizePegConvention,
  normalizeRankingUniverse,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  projectPegByConvention,
  RANKING_UNIVERSES,
  type PegGrowthConvention,
  valuationBasisLabel,
} from "@/contracts/strategyRun";
import { universeRecord } from "@/contracts/environmentContract.generated";
import { universeWithRole } from "@/contracts/universes";

export const dynamic = "force-dynamic";

function buildRankingsHref(params: {
  cursor?: string | null;
  cutoff?: string;
  convention?: PegGrowthConvention;
  universe?: string | null;
}): string {
  const query = new URLSearchParams();
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.cutoff) query.set("cutoff", params.cutoff);
  if (params.convention && params.convention !== "historical_cagr") query.set("convention", params.convention);
  if (params.universe && params.universe !== universeWithRole("strategy")) query.set("universe", params.universe);
  const qs = query.toString();
  return qs.length > 0 ? `/research/rankings?${qs}` : "/research/rankings";
}

function PanelNodeTd({ node, formatted }: { node: WidePanelCell | null; formatted: string | null }) {
  return (
    <td className="px-4 py-3 font-mono tabular-nums" title={node?.value ?? undefined}>
      <div>{formatCell(formatted)}</div>
      {node?.confidence && (
        <div className="text-[11px] text-gray-500" title={node.confidence}>
          conf {formatCell(formatRatio(node.confidence))}
        </div>
      )}
    </td>
  );
}

const pillClass = (active: boolean): string =>
  active
    ? "border border-sky-500 bg-sky-950/70 text-sky-300"
    : "border border-border bg-slate-900 text-gray-400 hover:border-gray-600 hover:text-gray-200";

export default async function RankingsPage({
  searchParams,
}: {
  searchParams: Promise<{ cursor?: string; cutoff?: string; convention?: string; universe?: string }>;
}) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Frankings");
  const params = await searchParams;
  const convention = normalizePegConvention(params.convention);
  const selectedUniverse = normalizeRankingUniverse(params.universe);
  const state = await loadRanking(principal.context, {
    cutoffAt: params.cutoff,
    cursor: params.cursor ?? null,
    universe: params.universe,
  });
  const names = await loadEntityDisplayMap();
  const basisLabel = valuationBasisLabel(
    state.kind === "ready"
      ? (state.data.rows.find((row) => row.valuationBasis !== null)?.valuationBasis ?? null)
      : null,
  );
  const headers = [
    "Rank",
    "Issuer",
    "Tier",
    "Current P/S",
    `Current ${basisLabel}`,
    `Target ${basisLabel}`,
    `Valuation gap (${basisLabel})`,
    `PEG (${PEG_CONVENTION_LABELS[convention]})`,
    "PEG rank",
    "Confidence",
    "Availability",
    "Inputs",
  ];

  return (
    <section aria-labelledby="rankings-heading" className="space-y-6">
      <div>
        <h1 id="rankings-heading" className="text-2xl font-bold tracking-tight">
          Valuation rankings
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Large-model-value universe ranked by valuation gap from the materialized strategy run. Revenue-share purity is on{" "}
          <Link href="/research/themes" className="text-accent hover:underline">
            Theme purity
          </Link>
          .
        </p>
      </div>

      <ReadStateNotice state={state} />
      <ClaimCeilingBanner />

      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2" data-testid="peg-convention-selector">
          <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
            Growth-Rate Convention (Module 1):
          </span>
          {PEG_GROWTH_CONVENTIONS.map((item) => (
            <Link
              key={item}
              href={buildRankingsHref({ cutoff: params.cutoff, convention: item, universe: selectedUniverse })}
              data-peg-convention={item}
              aria-current={item === convention ? "page" : undefined}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${pillClass(item === convention)}`}
            >
              {PEG_CONVENTION_LABELS[item]} ({item})
            </Link>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2" data-testid="universe-selector">
          <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">Universe Scope:</span>
          {RANKING_UNIVERSES.map((univ) => (
            <Link
              key={univ}
              href={buildRankingsHref({ cutoff: params.cutoff, convention, universe: univ })}
              data-universe-selector={univ}
              title={universeRecord(univ).label}
              aria-current={univ === selectedUniverse ? "page" : undefined}
              className={`rounded-md px-2.5 py-1 font-mono text-xs font-medium uppercase transition ${pillClass(univ === selectedUniverse)}`}
            >
              {univ.toUpperCase()}
            </Link>
          ))}
        </div>
      </div>

      {state.kind === "ready" && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="text-sm text-gray-500">
              Cutoff {state.data.cutoffAt} — {state.data.page.total} member(s) · Convention:{" "}
              <span className="font-mono text-gray-300">{convention}</span> · Universe:{" "}
              <span className="font-mono uppercase text-gray-300">{state.data.universe}</span>
            </p>
            <CardExportButton cutoff={params.cutoff} convention={convention} universe={state.data.universe} />
          </div>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Ranked issuers for the large-model-value theme at {state.data.cutoffAt}</caption>
              <thead className="bg-card text-xs uppercase text-gray-500">
                <tr>
                  {headers.map((h) => (
                    <th key={h} scope="col" className="px-4 py-3">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {state.data.rows.map((row) => {
                  const { peg: rowPeg, pegRank: rowPegRank, pegReasonCodes: pegReasons } = projectPegByConvention(
                    row,
                    convention,
                  );
                  return (
                    <tr key={row.issuerId} className="border-t border-border">
                      <td className="px-4 py-3">{formatCell(row.rank)}</td>
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
                      <td className="px-4 py-3">{formatTierLabel(row.tier)}</td>
                      <td className="px-4 py-3 tabular-nums" title={row.currentPriceToSales ?? undefined}>
                        {formatCell(formatRatio(row.currentPriceToSales))}
                      </td>
                      <td className="px-4 py-3 tabular-nums" title={row.currentMultiple ?? undefined}>
                        {formatCell(formatRatio(row.currentMultiple))}
                      </td>
                      <td className="px-4 py-3 tabular-nums" title={row.targetMultiple ?? undefined}>
                        {formatCell(formatRatio(row.targetMultiple))}
                      </td>
                      <td className={`px-4 py-3 tabular-nums ${signColor(row.valuationGap)}`} title={row.valuationGap ?? undefined}>
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

          <div className="space-y-3" data-testid="rankings-gppe-variants-section">
            <div>
              <h2 className="text-lg font-semibold text-gray-200">
                GPPE Factor Variants &amp; Standardized Wide-Panel Inputs (Module 2)
              </h2>
              <p className="mt-1 text-xs text-gray-400">
                Side-by-side GPPE factor variants and underlying wide-panel columns read from{" "}
                <code className="font-mono text-gray-300">mart.metric_node_values</code>.
              </p>
            </div>
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full text-left text-sm" data-testid="rankings-gppe-variants-table">
                <caption className="sr-only">
                  Side-by-side GPPE factor variants and wide-panel inputs at {state.data.cutoffAt}
                </caption>
                <thead className="bg-card text-xs uppercase text-gray-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">Issuer</th>
                    <th scope="col" className="px-4 py-3">Active Variant</th>
                    {[...GPPE_FACTOR_VARIANT_KEYS, ...RANKING_PANEL_INPUT_KEYS].map((k) => (
                      <th key={k} scope="col" className="px-4 py-3 font-mono">
                        {k}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {state.data.rows.map((row) => (
                    <tr key={row.issuerId} className="border-t border-border">
                      <th scope="row" className="px-4 py-3 font-medium">
                        <Link
                          href={`/research/entities/${encodeURIComponent(row.issuerId)}`}
                          className="text-accent hover:underline"
                          title={row.issuerId}
                        >
                          {entityLabel(row.issuerId, names)}
                        </Link>
                      </th>
                      <td className="px-4 py-3 font-mono text-xs text-sky-400">
                        {formatCell(row.laborEfficiencyMetric ?? null)}
                      </td>
                      {GPPE_FACTOR_VARIANT_KEYS.map((variantKey) => {
                        const node = row.gppeVariants?.[variantKey] ?? null;
                        return <PanelNodeTd key={variantKey} node={node} formatted={formatUsdMagnitude(node?.value ?? null)} />;
                      })}
                      {RANKING_PANEL_INPUT_KEYS.map((inputKey) => {
                        const node = row.panelInputs?.[inputKey] ?? null;
                        const formatted =
                          inputKey === "employees_total"
                            ? formatNumberMagnitude(node?.value ?? null)
                            : formatUsdMagnitude(node?.value ?? null);
                        return <PanelNodeTd key={inputKey} node={node} formatted={formatted} />;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
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
