/**
 * Modules 3 to 6 read their output counts at read time (#1175).
 *
 * Runs the real StrategyRunReadAdapter.overview() against a fake mart client. The fake answers
 * each count query from a fixture keyed by the table the SQL reads. It refuses any table it
 * does not know. The test proves:
 *  - each module reads its own table, and its numbers reach that module's card;
 *  - an empty table reads as unavailable with a zero count, never as a missing output;
 *  - a run that holds only fill rows (no available row) reads as unavailable;
 *  - modules 1, 2 and 7 keep their decision coverage and carry no output count;
 *  - a failed count query fails the read. It never turns into a zero.
 */

import {
	FixtureStrategyRunRepository,
	type AccessContext,
} from "../src/contracts/strategyRun";
import {
	StrategyRunReadAdapter,
	type ModuleOverviewRow,
} from "../src/server/mart/research-read";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";
import { readFileSync } from "node:fs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const CONTEXT: AccessContext = {
	contextId: "ctx:module-output",
	principalId: "principal:test-owner",
	tenantId: "tenant:truealpha",
	sessionId: "session:module-output",
	authenticationMethod: "password",
	issuedAt: "2026-01-01T00:00:00Z",
	expiresAt: "2026-01-01T01:00:00Z",
};

type CountRow = {
	run_id: string | null;
	row_count: number;
	available_count: number;
	latest_cutoff: string | null;
};

const COUNTS: Record<string, CountRow> = {
	"mart.issuer_supply_chain_exposure": {
		run_id: "run-supply",
		row_count: 41,
		available_count: 38,
		latest_cutoff: "2026-10-01T00:00:00Z",
	},
	"mart.issuer_analyst_ratings": {
		run_id: "run-analyst",
		row_count: 7,
		available_count: 7,
		latest_cutoff: "2026-10-02T00:00:00Z",
	},
	"mart.fund_virtual_company": {
		run_id: null,
		row_count: 0,
		available_count: 0,
		latest_cutoff: null,
	},
	// Fill rows only: twelve rows, none available. The module must not read as available.
	"mart.issuer_theme_purity": {
		run_id: "run-purity",
		row_count: 12,
		available_count: 0,
		latest_cutoff: "2026-10-03T00:00:00Z",
	},
};

function fakeClient(
	counts: Record<string, CountRow> = COUNTS,
	failOn: string | null = null,
): { client: MartClientLike; sqls: string[] } {
	const sqls: string[] = [];
	const client: MartClientLike = {
		async query(sql: string) {
			sqls.push(sql);
			const match = /from\s+(mart\.[a-z_]+)/.exec(sql);
			assert(match !== null, `count query names no mart table: ${sql.slice(0, 80)}`);
			const table = match[1];
			if (table === failOn) throw new Error(`simulated read failure on ${table}`);
			assert(
				Object.prototype.hasOwnProperty.call(counts, table),
				`fake client got a query for an unknown table: ${table}`,
			);
			return { rows: [counts[table]] };
		},
	};
	return { client, sqls };
}

function adapterWith(client: MartClientLike): StrategyRunReadAdapter {
	return new StrategyRunReadAdapter(
		{
			getLatest: (strategyId, context) =>
				new FixtureStrategyRunRepository().getLatest(strategyId, context),
		},
		(fn) => fn(client),
	);
}

function moduleRow(modules: readonly ModuleOverviewRow[], moduleNumber: number): ModuleOverviewRow {
	const row = modules.find((candidate) => candidate.module === moduleNumber);
	assert(row !== undefined, `module ${moduleNumber} is missing from the overview`);
	return row;
}

// --- modules 3 to 6: each card carries its own table's numbers ---
{
	const { client, sqls } = fakeClient();
	const modules = await adapterWith(client).overview(CONTEXT);

	const supply = moduleRow(modules, 3);
	assert(supply.source === "mart.issuer_supply_chain_exposure", `module 3 source is ${supply.source}`);
	assert(supply.output !== null, "module 3 carries no output count");
	assert(supply.output.table === "mart.issuer_supply_chain_exposure", "module 3 counts the wrong table");
	assert(supply.output.rows === 41, `module 3 rows: expected 41, got ${supply.output.rows}`);
	assert(supply.output.availableRows === 38, `module 3 available rows: expected 38, got ${supply.output.availableRows}`);
	assert(supply.output.latestCutoff === "2026-10-01T00:00:00Z", "module 3 latest cutoff is not its table's");
	assert(supply.output.runId === "run-supply", "module 3 run id is not its table's");
	assert(supply.availability === "available", "module 3 with available rows must read available");
	assert(supply.coverage === null, "module 3 has no decision coverage");

	const analyst = moduleRow(modules, 4);
	assert(analyst.source === "mart.issuer_analyst_ratings", `module 4 source is ${analyst.source}`);
	assert(analyst.output?.rows === 7, "module 4 must show its own seven rows, not module 3's");
	assert(analyst.output?.latestCutoff === "2026-10-02T00:00:00Z", "module 4 latest cutoff is not its table's");
	assert(analyst.availability === "available", "module 4 with available rows must read available");

	const fund = moduleRow(modules, 5);
	assert(fund.source === "mart.fund_virtual_company", `module 5 source is ${fund.source}`);
	assert(fund.output !== null, "an empty table must still carry a count object");
	assert(fund.output.rows === 0, "an empty table reads as zero rows");
	assert(fund.output.latestCutoff === null, "an empty table has no latest cutoff");
	assert(fund.output.runId === null, "an empty table has no run id");
	assert(fund.availability === "unavailable", "an empty table must read unavailable");

	const purity = moduleRow(modules, 6);
	assert(purity.source === "mart.issuer_theme_purity", `module 6 source is ${purity.source}`);
	assert(purity.output?.rows === 12, "module 6 must show its twelve rows");
	assert(purity.output?.availableRows === 0, "module 6 must show zero available rows");
	assert(
		purity.availability === "unavailable",
		"a run of fill rows only must read unavailable, not available",
	);

	// One count query per output table, each against its own table and nothing else.
	assert(sqls.length === 4, `expected four count queries, got ${sqls.length}`);
	for (const table of [
		"mart.issuer_supply_chain_exposure",
		"mart.issuer_analyst_ratings",
		"mart.fund_virtual_company",
		"mart.issuer_theme_purity",
	]) {
		assert(
			sqls.some((sql) => new RegExp(`from\\s+${table.replace(/\./g, "\\.")}\\b`).test(sql)),
			`no count query reads ${table}`,
		);
	}
}

// --- modules 1, 2 and 7: decision coverage is unchanged and no output count is attached ---
{
	const { client } = fakeClient();
	const modules = await adapterWith(client).overview(CONTEXT);
	for (const moduleNumber of [1, 2, 7]) {
		const row = moduleRow(modules, moduleNumber);
		assert(row.source === "mart.strategy_decisions", `module ${moduleNumber} must read the decision table`);
		assert(row.output === null, `module ${moduleNumber} must not carry an output count`);
		assert(row.coverage !== null, `module ${moduleNumber} must keep its decision coverage`);
		assert(row.coverage.total > 0, `module ${moduleNumber} coverage must count the run's decisions`);
		const expected = row.coverage.withValue > 0 ? "available" : "unavailable";
		assert(row.availability === expected, `module ${moduleNumber} availability must follow its coverage`);
	}
	// The fixture carries no PEG value, so module 1 must still read unavailable.
	assert(moduleRow(modules, 1).availability === "unavailable", "module 1 availability changed");
}

// --- a failed count query fails the read; it never reports a zero ---
{
	const { client } = fakeClient(COUNTS, "mart.issuer_analyst_ratings");
	let threw = false;
	try {
		await adapterWith(client).overview(CONTEXT);
	} catch {
		threw = true;
	}
	assert(threw, "a failed count query must reject the overview read");
}

// --- the page reads the new fields, so a count never stays invisible ---
{
	const page = readFileSync(new URL("../src/app/research/page.tsx", import.meta.url), "utf8");
	assert(/\.source\b/.test(page), "the module card must render module.source");
	assert(/\.output\b/.test(page), "the module card must render module.output");
}

console.log("ok  modules 3-6 read their own output counts; modules 1, 2, 7 unchanged");
