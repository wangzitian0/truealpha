import Link from "next/link";

import type { RankingRow } from "@/server/mart/research-read";

/** What a published number was computed FROM, on the surface rather than in an audit.
 *
 * The cell this replaces printed a full trace id — `mart:8cdb081d887f:issuer:lei:
 * BQ4BKCS1HXDV9HN80Z93:2026-08-18T23:47:25.196906Z` — as the widest column in the
 * table, which told a reader nothing they could act on while the facts that matter
 * were not on the page at all.
 *
 * init.md, section 0: "Every step downstream has to be traceable back to the
 * original raw material — that's the reason the point-in-time principle exists,
 * not a technical purity fetish."
 *
 * The summary shows the operating period end, because that is the value that
 * silently drifts: across the run serving /research today the period ends span
 * 2025-08-31 to 2026-01-25, five months of spread behind one "current P/S"
 * column. #529 was the same shape — a rank-1 holding at 50% weight, selected on
 * a 2010 share count that the surface reported as fresh.
 *
 * `<details>` rather than a click handler: this stays a server component, and a
 * reader with JavaScript off still gets every field.
 */
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
					<div key={label} className="flex gap-2">
						<dt className="w-20 shrink-0 text-gray-600">{label}</dt>
						<dd className={value === null ? "text-amber-500" : ""}>
							{value ?? "unknown"}
						</dd>
					</div>
				))}
				<Field label="Universe" value={p.universeVersion} />
				<Field label="GPPE def" value={shorten(p.gppeDefinitionSha256)} />
				<Field label="Tier def" value={shorten(p.tierDefinitionSha256)} />
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

function Field({ label, value }: { label: string; value: string | null }) {
	return (
		<div className="flex gap-2">
			<dt className="w-20 shrink-0 text-gray-600">{label}</dt>
			<dd className={value === null ? "text-amber-500" : ""}>
				{value ?? "unknown"}
			</dd>
		</div>
	);
}

/** A 64-character digest in a table cell is noise; the first 12 identify it and
 *  the full value stays available on hover for anyone comparing two runs. */
function shorten(sha: string | null): string | null {
	return sha === null ? null : sha.slice(0, 12);
}
