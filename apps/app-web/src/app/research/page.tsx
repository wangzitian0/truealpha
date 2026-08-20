import Link from "next/link";
import { redirect } from "next/navigation";
import {
	formatPercentFromFraction,
	formatSignedRatio,
	signColor,
} from "@/client/format";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { getServerPrincipal } from "@/server/auth/request-context";
import { loadOverview } from "@/server/dashboard";
import {
	entityLabel,
	loadEntityDisplayMap,
} from "@/server/mart/entity-resolution";
import { loadStrategyRunPage } from "@/server/strategy-page";

export const dynamic = "force-dynamic";

export default async function ResearchOverviewPage() {
	const principal = await getServerPrincipal();
	if (!principal) redirect("/login?from=%2Fresearch");
	const state = await loadOverview(principal.context);
	const strategy = await loadStrategyRunPage(principal, "large_model_value_v0");
	const names = await loadEntityDisplayMap();
	const selections =
		strategy.kind === "ready"
			? strategy.report.decisions.filter(
					(decision) => decision.outcome === "selected",
				)
			: [];
	const evaluated =
		strategy.kind === "ready" ? strategy.report.decisions.length : 0;
	const latestCutoff = selections[0]?.cutoff_at ?? null;

	return (
		<section aria-labelledby="overview-heading" className="space-y-8">
			<div>
				<h1 id="overview-heading" className="text-3xl font-bold tracking-tight">
					Today
				</h1>
				<p className="mt-2 text-gray-400">
					{latestCutoff
						? `Data as of ${latestCutoff} — the governed run every surface (web, MCP, chat) resolves.`
						: "No strategy run recorded yet."}
				</p>
			</div>

			<ReadStateNotice state={state} />

			{selections.length > 0 && (
				<>
					<div className="overflow-x-auto rounded-xl border border-border">
						<table className="w-full text-left text-sm">
							<caption className="sr-only">Current strategy selections</caption>
							<thead className="bg-card text-xs uppercase text-gray-500">
								<tr>
									<th scope="col" className="px-4 py-3">
										Selected
									</th>
									<th scope="col" className="px-4 py-3">
										Tier
									</th>
									<th scope="col" className="px-4 py-3">
										Valuation gap
									</th>
									<th scope="col" className="px-4 py-3">
										Weight
									</th>
									<th scope="col" className="px-4 py-3">
										Trace
									</th>
								</tr>
							</thead>
							<tbody>
								{selections.map((decision) => (
									<tr
										key={decision.issuer_id}
										className="border-t border-border"
									>
										<th
											scope="row"
											className="px-4 py-3 font-medium"
											title={decision.issuer_id}
										>
											{entityLabel(decision.issuer_id, names)}
										</th>
										<td className="px-4 py-3">{decision.tier ?? "—"}</td>
										<td
											className={`px-4 py-3 tabular-nums ${signColor(decision.valuation_gap)}`}
											title={decision.valuation_gap ?? undefined}
										>
											{formatSignedRatio(decision.valuation_gap) ?? "—"}
										</td>
										<td className="px-4 py-3 tabular-nums">
											{formatPercentFromFraction(decision.target_weight) ?? "—"}
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
							</tbody>
						</table>
					</div>
					<p className="text-sm text-gray-500">
						{selections.length} selected of {evaluated} evaluated —{" "}
						<Link
							href="/research/strategy"
							className="text-accent hover:underline"
						>
							full decisions
						</Link>{" "}
						·{" "}
						<Link
							href="/research/coverage"
							className="text-accent hover:underline"
						>
							why some companies are missing
						</Link>
					</p>
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
							<li
								key={module.module}
								className="rounded-xl border border-border bg-card p-5"
							>
								<div className="flex items-center justify-between">
									<span className="text-sm text-gray-500">
										Module {module.module}
									</span>
									<span className="rounded-full border border-border px-2 py-0.5 text-xs text-gray-400">
										{module.gate}
									</span>
								</div>
								<h2 className="mt-2 font-semibold">{module.name}</h2>
								<p className="mt-1 text-sm text-gray-400">{module.note}</p>
								<div className="mt-3">
									<AvailabilityBadge status={module.availability} />
								</div>
							</li>
						))}
					</ul>
					<p className="text-sm text-gray-500">
						Explore the{" "}
						<Link
							href="/research/rankings"
							className="text-accent hover:underline"
						>
							theme rankings
						</Link>{" "}
						or{" "}
						<Link
							href="/research/compare"
							className="text-accent hover:underline"
						>
							issuer comparison
						</Link>
						.
					</p>
				</>
			)}
		</section>
	);
}
