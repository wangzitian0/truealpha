/**
 * Static properties of this app's source, in one scanner — #584.
 *
 * Four tests had the same mechanic: read a file, strip comments and string
 * literals, run a regex, assert. `assert` was written out four times,
 * `listFilesRecursive` twice with two different implementations, and the
 * stripping three times with three different depths — one of which carried a
 * comment saying it used "the same stripping order" as another, which is a
 * copy admitting it is a copy. When `admin-read-state` needed to match a string
 * literal it had to opt out of the stripping by re-reading the file, a decision
 * invisible from the other three.
 *
 * The rules themselves are NOT uniform and are not forced into a table: two are
 * "this pattern must not appear", one is "this file must reach this component",
 * one is structural (count returns between two positions). Only the mechanics
 * are shared. Every failure message is the original, verbatim — each was
 * written to explain a specific defect and explaining it is most of its value.
 *
 * Run standalone: `bun run tests/source-contracts.test.ts`.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

function listFiles(dir: string): string[] {
	const out: string[] = [];
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		out.push(
			...(statSync(full).isDirectory()
				? listFiles(full)
				: /\.tsx?$/.test(entry)
					? [full]
					: []),
		);
	}
	return out;
}

/** Comments only. For rules whose subject IS a string literal. */
function withoutComments(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
}

/** Comments and string/template literals, so the scan sees executable code —
 * comments and paths contain '/' and '*' legitimately, and prose contains words
 * like "return".
 *
 * The ORDER is load-bearing and is the original's: block comments (which covers
 * JSX `{/* … *\/}` too), then string literals, then line comments. Composing
 * this out of `withoutComments` instead — strings first, then all comments —
 * changed the result and the scan immediately failed on a `**` that had been
 * inside a comment. Left explicit rather than factored. */
function executableCode(source: string): string {
	return source
		.replace(/\/\*[\s\S]*?\*\//g, " ")
		.replace(/`[^`]*`/g, " ")
		.replace(/"[^"]*"/g, " ")
		.replace(/'[^']*'/g, " ")
		.replace(/\/\/[^\n]*/g, " ");
}

// ─── #370/#433: the App does deterministic reformatting only ─────────────────
//
// Every mart read adapter may sort, filter, paginate, label and copy
// already-materialized values through byte-exact. It must never join two
// factors or two time points into a new metric in the Next.js backend (init.md
// Section 1, rule 2). Index arithmetic for pagination lives in the separate
// `pagination.ts`, which this scan deliberately does not target.
{
	const ADAPTER_PATHS = [
		"src/server/mart/research-read.ts",
		"src/server/mart/topt-gppe-repository.ts",
	];

	for (const relativePath of ADAPTER_PATHS) {
		const rawSource = readFileSync(join(process.cwd(), relativePath), "utf8");
		const code = executableCode(rawSource);

		// '+' and '-' are intentionally allowed (string joins, negatives);
		// multiplication, division and modulo have no legitimate reformatting use.
		const FORBIDDEN_OPERATORS: readonly [RegExp, string][] = [
			[/\*\*/, "exponentiation '**'"],
			[/[^*]\*[^*/]/, "multiplication '*'"],
			[/[^/*]\/[^/*]/, "division '/'"],
			[/%/, "modulo '%'"],
		];
		for (const [pattern, label] of FORBIDDEN_OPERATORS) {
			assert(
				!pattern.test(code),
				`${relativePath} must not contain ${label} (cross-factor computation)`,
			);
		}

		const FORBIDDEN_CALLS: readonly string[] = [
			"Math.",
			"Number(",
			"parseFloat",
			"parseInt",
			".reduce(",
			"BigInt(",
		];
		for (const token of FORBIDDEN_CALLS) {
			assert(
				!code.includes(token),
				`${relativePath} must not call ${token} (numeric computation)`,
			);
		}

		assert(
			!/from\s+["'][^"']*decimal/i.test(rawSource),
			`${relativePath} must not import a decimal library`,
		);
	}

	// #370: a deployed route must never fall back to the fixture. A behavioural
	// test cannot tell "hit real mart, got no rows" from "silently read the
	// fixture" without depending on live database contents, so the wiring is
	// checked statically instead.
	const researchRead = withoutComments(
		readFileSync(
			join(process.cwd(), "src/server/mart/research-read.ts"),
			"utf8",
		),
	);
	assert(
		/repository\s*\?\?\s*new MartStrategyRunRepository\(\)/.test(researchRead),
		"StrategyRunReadAdapter's bare default must be MartStrategyRunRepository, not the fixture",
	);
	assert(
		!/new FixtureStrategyRunRepository\(\)/.test(researchRead),
		"the fixture repository must not appear as a bare default in the mart read adapter",
	);
}

// ─── #371/#493: research cannot import an administrator server module ────────
{
	// Matches `@/server/admin-<anything>` and `@/server/admin/<anything>` import
	// specifiers (also their relative `server/admin...` spellings).
	const ADMIN_IMPORT_PATTERN = /["'@/.]server\/admin[-/]/;

	const researchFiles = listFiles(join(process.cwd(), "src/app/research"));
	assert(
		researchFiles.length > 0,
		"expected at least one file under src/app/research to scan",
	);
	// The shared component/lib layers research pages can reach are covered too.
	const reachable = [
		...researchFiles,
		...listFiles(join(process.cwd(), "src/components")),
		...listFiles(join(process.cwd(), "src/client")),
	];
	for (const file of reachable) {
		assert(
			!ADMIN_IMPORT_PATTERN.test(readFileSync(file, "utf8")),
			`${file} must not import an administrator server module (src/server/admin-*)`,
		);
	}

	// Guard against the rule going vacuous in the OTHER direction: the moved
	// strategy loader must no longer match the admin convention (it is research
	// surface now), and must be imported by the research strategy page.
	statSync(join(process.cwd(), "src/server/strategy-page.ts"));
	const strategyPage = readFileSync(
		join(process.cwd(), "src/app/research/strategy/page.tsx"),
		"utf8",
	);
	assert(
		strategyPage.includes("@/server/strategy-page"),
		"sanity check failed: /research/strategy must import the research-side strategy loader",
	);
}

// ─── #495: one read-state vocabulary, one renderer ───────────────────────────
{
	const adminModules = listFiles(join(process.cwd(), "src/server/admin"));
	assert(
		adminModules.length > 0,
		"expected at least one module under src/server/admin to scan",
	);
	for (const file of adminModules) {
		// A hand-rolled read union always spells its success case `kind: "ready"`.
		// Comments stripped only — the string literal IS the thing being matched.
		assert(
			!/kind:\s*"ready"\s*;/.test(withoutComments(readFileSync(file, "utf8"))),
			`${file} declares its own read-outcome union (a \`kind: "ready";\` member). Administrator ` +
				`read surfaces must return \`ReadState<T>\` from @/server/read-state so they share the ` +
				`research side's states AND its words (#495).`,
		);
	}

	const adminPages = listFiles(join(process.cwd(), "src/app/admin")).filter(
		(f) => f.endsWith("page.tsx"),
	);
	assert(adminPages.length > 0, "expected at least one /admin page to scan");
	for (const file of adminPages) {
		const source = readFileSync(file, "utf8");
		// A page that never inspects a non-ready outcome has nothing to render a
		// notice for; one that does must use the shared renderer.
		if (
			!/kind\s*!==\s*"ready"|kind\s*===\s*"denied"|kind\s*===\s*"error"/.test(
				source,
			)
		)
			continue;
		assert(
			source.includes("ReadStateNotice"),
			`${file} branches on a non-ready outcome but does not render <ReadStateNotice>. Absence ` +
				`states must go through the one renderer whose words are unit-tested (#495).`,
		);
	}
}

// ─── #494 assertion (b): the claim ceiling is a page property ────────────────
//
// Between the page component and its banner there may be exactly ONE `return`
// — the outermost JSX return. Any early exit above the banner makes the count
// 2+ and fails here, at the file that caused it, instead of intermittently in
// the browser suite.
{
	const PAGES = [
		"src/app/research/rankings/page.tsx",
		"src/app/research/strategy/page.tsx",
	];

	for (const relativePath of PAGES) {
		const code = executableCode(
			readFileSync(join(process.cwd(), relativePath), "utf8"),
		);

		const componentAt = code.indexOf("export default");
		assert(componentAt !== -1, `${relativePath}: no default export found`);

		const bannerAt = code.indexOf("<ClaimCeilingBanner");
		assert(
			bannerAt !== -1,
			`${relativePath}: renders no <ClaimCeilingBanner />. Every page e2e/walk-tree.mjs ` +
				`asserts the claim ceiling on must render it — see #494 assertion (b).`,
		);
		assert(
			bannerAt > componentAt,
			`${relativePath}: <ClaimCeilingBanner /> appears before the page component.`,
		);

		const returnsBefore = (
			code.slice(componentAt, bannerAt).match(/\breturn\b/g) ?? []
		).length;
		assert(
			returnsBefore === 1,
			`${relativePath}: ${returnsBefore} return statements sit between the page component and ` +
				`<ClaimCeilingBanner />, so at least one data state renders the page WITHOUT the claim ` +
				`ceiling. Exactly one (the outermost JSX return) is allowed: render every non-ready ` +
				`state inside the returned tree instead of exiting above the banner (#494 assertion b).`,
		);
	}
}

console.log(
	"source contracts: no cross-factor computation in the mart adapters; research cannot reach " +
		"src/server/admin-*; one read-state vocabulary and renderer; the claim ceiling is unconditional",
);

// ---------------------------------------------------------------------------
// A declared contract field must have a source column.
//
// `StrategyRunDecision.confidence` was declared, parsed with a [0,1] range
// check, carried onto `RankingRow`, and rendered as a column header on
// /research/rankings. `mart.strategy_decisions` has no such column and
// DECISIONS_SQL never selected it, so every row in production showed an em
// dash — for as long as the column has existed. Nothing failed: the parser
// reads undefined, coerces to null, and null is a legal value.
//
// The value was never missing. `mart.topt_core_results.confidence` is populated
// on 1002 of 1002 production rows, 0 to 0.9, and joins to the served decisions
// 20 of 20 on (issuer_id, cutoff). It was simply never read.
// ---------------------------------------------------------------------------

{
	const source = (relative: string) =>
		readFileSync(join(process.cwd(), relative), "utf8");
	const select = source("src/server/mart/strategy-run-repository.ts")
		.split("const DECISIONS_SQL = `", 2)[1]
		.split("from ", 2)[0];

	const body = source("src/contracts/strategyRun.ts")
		.split("export interface StrategyRunDecision {", 2)[1]
		.split("\n}", 2)[0];
	const declared = [...body.matchAll(/^\s*([a-z_]+)\??:/gm)].map(
		(match) => match[1],
	);
	assert(
		declared.length > 10,
		`the decision contract parsed as ${declared.length} fields; this scan lost its subject`,
	);

	// `d.x`, `t.x`, a bare `x`, or `... as x`.
	const selected = new Set([
		...[...select.matchAll(/\b[a-z]\.([a-z_]+)/g)].map((m) => m[1]),
		...[...select.matchAll(/\bas\s+([a-z_]+)/g)].map((m) => m[1]),
		...[...select.matchAll(/(?:select|,)\s*([a-z_]+)\s*(?=,|$)/gm)].map(
			(m) => m[1],
		),
	]);

	const phantom = declared.filter((field) => !selected.has(field));
	assert(
		phantom.length === 0,
		`StrategyRunDecision declares ${phantom.join(", ")} and DECISIONS_SQL never selects it. ` +
			`The parser reads undefined, coerces to null, and the page renders an em dash forever — ` +
			`which is what "Confidence" did in every production row until it was joined from ` +
			`mart.topt_core_results.`,
	);
	console.log("ok  every declared decision field has a source column");

	// Selecting a column is half the path. `decisionFromRow` hard-coded
	// `confidence: null` under a stale comment, so the first cut of this join
	// selected the value and threw it away one function later: the SQL was right,
	// the page still rendered an em dash in every row, and the check above passed
	// the whole time. A field has to survive the mapper too (review).
	const mapper = source("src/server/mart/strategy-run-repository.ts")
		.split("function decisionFromRow", 2)[1]
		.split("\nfunction ", 2)[0];
	// Assigned AND sourced from the row. Asking only "is it assigned" passes on
	// `confidence: null`, which is the exact line that caused this — the guard
	// would have been green on the defect it is named after. Asking for `row.` in
	// the assignment itself is too strict the other way: `rank` is validated into
	// a local first. So: the field must be assigned, and the mapper must read
	// `row.<field>` somewhere. Both directions red-proven.
	const dropped = declared.filter(
		(field) =>
			!new RegExp(`\\b${field}\\s*:`).test(mapper) ||
			!mapper.includes(`row.${field}`),
	);
	assert(
		dropped.length === 0,
		`decisionFromRow never assigns ${dropped.join(", ")}. The SELECT returns the column and the ` +
			`mapper discards it, so the page shows nothing while every scan of the SQL passes.`,
	);
	console.log("ok  every declared decision field survives decisionFromRow");
}
