import Link from "next/link";
import { redirect } from "next/navigation";
import {
	formatCell,
	formatPercentFromFraction,
	formatRatio,
	formatSignedRatio,
	signColor,
} from "@/client/format";
import { summarizeUniverseUsability } from "@/contracts/exclusion-reasons";
import {
	currentMultipleOf,
	reportValuationBasis,
	targetMultipleOf,
	valuationBasisLabel,
} from "@/contracts/strategyRun";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { ResearchTable } from "@/components/research-table";
import { StrategyDefinitionLine } from "@/components/strategy-definition";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { getServerPrincipal } from "@/server/auth/request-context";
import { loadOverview } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { DASHBOARD_STRATEGY_ID } from "@/server/mart/research-read";
import { loadStrategyRunPage } from "@/server/strategy-page";
import { describeServedRunAge } from "./served-run-age";

export const dynamic = "force-dynamic";

export default async function ResearchOverviewPage() {
	const principal = await getServerPrincipal();
	if (!principal) redirect("/login?from=%2Fresearch");
	const state = await loadOverview(principal.context);
	const strategy = await loadStrategyRunPage(principal, DASHBOARD_STRATEGY_ID);
	const names = await loadEntityDisplayMap();
	const usability = summarizeUniverseUsability(
		strategy.kind === "ready" ? strategy.report.decisions : [],
	);
	const { selections, evaluatedCount: evaluated } = usability;
	const basisLabel = valuationBasisLabel(reportValuationBasis(selections));
	const latestCutoff =
		selections[0]?.cutoff_at ??
		(strategy.kind === "ready" ? (strategy.report.decisions[0]?.cutoff_at ?? null) : null);
	const governed =
		strategy.kind === "ready" && "governed" in strategy.report && strategy.report.governed === true;
	const age = latestCutoff ? describeServedRunAge(latestCutoff) : null;
	const selectionHeaders = [
		"Selected",
		"Tier",
		`Current ${basisLabel}`,
		`Target ${basisLabel}`,
		`Valuation gap (${basisLabel})`,
		"Weight",
		"Trace",
	];

	return (
		<section aria-labelledby="overview-heading" className="space-y-8">
			<div>
				<h1 id="overview-heading" className="text-3xl font-bold tracking-tight">
					Today
				</h1>
				<p className="mt-2 text-gray-400">
					{latestCutoff
						? governed
							? `Data as of ${latestCutoff} — the run the governed capture head resolves to; MCP and chat read the same one.`
							: `Data as of ${latestCutoff} — the newest recorded run; no governed capture head resolves a strategy run on this database yet.`
						: strategy.kind === "empty"
							? "No decisions recorded for this strategy run."
							: "No strategy run recorded yet."}
					{age && (
						<>
							{" "}
							<span
								data-testid="served-run-age"
								data-stale={age.stale ? "true" : "false"}
								className={`ml-2 rounded px-2 py-0.5 text-xs font-medium ${
									age.stale ? "bg-amber-500/15 text-amber-300" : "bg-gray-500/15 text-gray-300"
								}`}
								title={
									age.stale
										? "Older than one scheduled cycle plus slack (36 h) — the same threshold the nightly pointer invariant raises."
										: "Within one scheduled cycle plus slack (36 h)."
								}
							>
								{age.stale ? `stale · ${age.label}` : age.label}
							</span>
						</>
					)}
				</p>
				{strategy.kind === "ready" && <StrategyDefinitionLine definition={strategy.report.definition} />}
			</div>

			<ReadStateNotice state={state} />

			{selections.length > 0 && (
				<>
					<ResearchTable caption="Current strategy selections" headers={selectionHeaders}>
						{selections.map((decision) => (
							<tr key={decision.issuer_id} className="border-t border-border">
								<th scope="row" className="px-4 py-3 font-medium">
									<Link
										href={`/research/entities/${encodeURIComponent(decision.issuer_id)}`}
										className="text-accent hover:underline"
										title={decision.issuer_id}
									>
										{entityLabel(decision.issuer_id, names)}
									</Link>
								</th>
								<td className="px-4 py-3">{formatCell(decision.tier)}</td>
								<td className="px-4 py-3 tabular-nums" title={currentMultipleOf(decision) ?? undefined}>
									{formatCell(formatRatio(currentMultipleOf(decision)))}
								</td>
								<td className="px-4 py-3 tabular-nums" title={targetMultipleOf(decision) ?? undefined}>
									{formatCell(formatRatio(targetMultipleOf(decision)))}
								</td>
								<td
									className={`px-4 py-3 tabular-nums ${signColor(decision.valuation_gap)}`}
									title={decision.valuation_gap ?? undefined}
								>
									{formatCell(formatSignedRatio(decision.valuation_gap))}
								</td>
								<td className="px-4 py-3 tabular-nums" title={decision.target_weight ?? undefined}>
									{formatCell(formatPercentFromFraction(decision.target_weight))}
								</td>
								<td className="px-4 py-3">
									<Link
										className="text-accent hover:underline"
										href={`/research/trace?issuer=${encodeURIComponent(decision.issuer_id)}&cutoff=${encodeURIComponent(decision.cutoff_at)}`}
									>
										trace
									</Link>
								</td>
							</tr>
						))}
					</ResearchTable>
					<p className="text-sm text-gray-500" data-testid="universe-usability-summary">
						{selections.length} selected · {usability.usableCount} usable of {evaluated} evaluated
						{usability.excludedCount > 0 ? ` (${usability.excludedCount} excluded)` : ""} —{" "}
						<Link href="/research/strategy" className="text-accent hover:underline">
							full decisions
						</Link>{" "}
						·{" "}
						<Link href="/research/coverage" className="text-accent hover:underline">
							why some companies are missing
						</Link>
					</p>
					{usability.excluded.length > 0 && (
						<div
							className="rounded-lg border border-border bg-card/50 p-4 text-xs text-gray-400"
							data-testid="excluded-issuers-disclosure"
						>
							<p className="font-medium text-gray-300">
								Excluded from the {evaluated}-issuer universe ({usability.usableCount} usable):
							</p>
							<ul className="mt-2 space-y-1">
								{usability.excluded.map((item) => (
									<li key={item.issuer_id}>
										<span className="font-medium text-gray-200" title={item.issuer_id}>
											{entityLabel(item.issuer_id, names)}
										</span>
										{item.exclusion_reason && (
											<>
												{" "}
												<code className="ml-1.5 rounded bg-gray-500/15 px-1.5 py-0.5 font-mono text-[11px] text-gray-300">
													{item.exclusion_reason}
												</code>
											</>
										)}{" "}
										— {item.label}
									</li>
								))}
							</ul>
						</div>
					)}
					<ClaimCeilingBanner />
				</>
			)}

			{state.kind === "ready" && (
				<>
					<p className="text-sm text-gray-500">
						{state.data.run.strategyRunId
							? `Run ${state.data.run.strategyRunId} · executed ${state.data.run.executedAt ?? "unknown"} · source ${state.data.run.source}. `
							: `Source ${state.data.run.source} (no persisted run id). `}
						{state.data.latestCutoff
							? `Latest materialized cutoff: ${state.data.latestCutoff}.`
							: "No materialized cutoff yet."}
					</p>
					<ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
						{state.data.modules.map((module) => (
							<li key={module.module} className="rounded-xl border border-border bg-card p-5">
								<div className="flex items-center justify-between">
									<span className="text-sm text-gray-500">Module {module.module}</span>
									<span className="rounded-full border border-border px-2 py-0.5 text-xs text-gray-400">
										{module.gate}
									</span>
								</div>
								<h2 className="mt-2 font-semibold">{module.name}</h2>
								<p className="mt-1 text-sm text-gray-400">{module.note}</p>
								<p className="mt-2 text-xs text-gray-500">
									Source <code className="font-mono">{module.source}</code>
								</p>
								{module.output !== null && (
									<p className="mt-1 text-xs text-gray-500 tabular-nums" data-testid="module-output">
										{module.output.rows} rows · {module.output.availableRows} available · latest cutoff{" "}
										{formatCell(module.output.latestCutoff)}
									</p>
								)}
								<div className="mt-3 flex items-center gap-2">
									<AvailabilityBadge status={module.availability} />
									{module.coverage !== null && module.coverage.total > 0 && (
										<span
											className="text-xs text-gray-500 tabular-nums"
											title={`${module.coverage.withValue} of ${module.coverage.total} decisions in this run carry a value`}
										>
											{Math.round((module.coverage.withValue / module.coverage.total) * 100)}% ·{" "}
											{module.coverage.withValue}/{module.coverage.total}
										</span>
									)}
								</div>
							</li>
						))}
					</ul>
					<p className="text-sm text-gray-500">
						Explore the{" "}
						<Link href="/research/rankings" className="text-accent hover:underline">
							theme rankings
						</Link>{" "}
						or{" "}
						<Link href="/research/compare" className="text-accent hover:underline">
							issuer comparison
						</Link>
						.
					</p>
				</>
			)}
		</section>
	);
}
