import Link from "next/link";
import { formatShortSha } from "@/client/format";
import type { RankingRow } from "@/server/mart/research-read";

export function ProvenanceCell({ row }: { row: RankingRow }) {
	const { provenance: p } = row;
	const vintages: [string, string | null][] = [
		["Operating", p.operatingPeriodEnd],
		["Revenue", p.revenuePeriodEnd],
		["Shares", p.sharesPeriodEnd],
	];
	const known = vintages
		.filter(([, value]) => value !== null)
		.map(([, value]) => value as string);
	// An absent vintage is information, not a blank: "we do not know when this was
	// true" is exactly what a reader needs in order to distrust the number.
	const summary =
		p.operatingPeriodEnd ?? (known.length > 0 ? known[0] : "vintage unknown");

	return (
		<details className="group" data-evidence="true">
			<summary className="cursor-pointer list-none font-mono text-xs text-gray-400 hover:text-accent">
				<span className="underline decoration-dotted underline-offset-2">
					{summary}
				</span>
				<span className="ml-1 text-gray-600 group-open:hidden">▸</span>
				<span className="ml-1 hidden text-gray-600 group-open:inline">▾</span>
			</summary>
			<dl className="mt-2 space-y-1 font-mono text-[11px] leading-relaxed text-gray-400">
				{vintages.map(([label, value]) => (
					<Field key={label} label={label} value={value} />
				))}
				<Field label="Universe" value={p.universeVersion} />
				<Field
					label="GPPE def"
					value={p.gppeDefinitionSha256 ? formatShortSha(p.gppeDefinitionSha256) : null}
					title={p.gppeDefinitionSha256}
				/>
				<Field
					label="Tier def"
					value={p.tierDefinitionSha256 ? formatShortSha(p.tierDefinitionSha256) : null}
					title={p.tierDefinitionSha256}
				/>
			</dl>
			<Link
				href={`/research/trace?issuer=${encodeURIComponent(row.issuerId)}&cutoff=${encodeURIComponent(row.cutoffAt)}`}
				className="mt-2 inline-block font-mono text-[11px] text-accent hover:underline"
			>
				full trace →
			</Link>
		</details>
	);
}

function Field({
	label,
	value,
	title,
}: {
	label: string;
	value: string | null;
	title?: string | null;
}) {
	return (
		<div className="flex gap-2">
			<dt className="w-20 shrink-0 text-gray-600">{label}</dt>
			<dd className={value === null ? "text-amber-500" : ""} title={title ?? undefined}>
				{value ?? "unknown"}
			</dd>
		</div>
	);
}
