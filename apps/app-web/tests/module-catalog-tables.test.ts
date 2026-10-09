/**
 * Static guard for the module catalog's source tables (#1175).
 *
 * Every catalog module names the table its output is read from. Each of those tables must
 * have a `create table` statement in db/migrations, so a catalog entry cannot point at a
 * relation the schema never creates. SQL comments are stripped first: a commented-out
 * `create table` is prose, not a relation.
 *
 * Mutation check (recorded in the PR): rename one mapped table in MODULE_CATALOG. This file
 * then turns red, because the renamed table has no `create table` in the migrations.
 */

import { readdirSync, readFileSync } from "node:fs";

import {
	MODULE_CATALOG,
	moduleSourceTable,
} from "../src/server/mart/research-read";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const MIGRATIONS_DIR = new URL("../../../db/migrations/", import.meta.url);

const migrationSql = readdirSync(MIGRATIONS_DIR)
	.filter((name) => name.endsWith(".sql"))
	.sort()
	.map((name) =>
		readFileSync(new URL(name, MIGRATIONS_DIR), "utf8").replace(/--[^\n]*/g, " "),
	)
	.join("\n");

function createsTable(table: string): boolean {
	const escaped = table.replace(/\./g, "\\.");
	const pattern = new RegExp(
		`create\\s+table\\s+(if\\s+not\\s+exists\\s+)?${escaped}\\s*\\(`,
		"i",
	);
	return pattern.test(migrationSql);
}

// Non-vacuity: the guard reads real migrations and the real seven-module catalog.
assert(
	/create\s+table/i.test(migrationSql),
	"the migration corpus contains no create table statement; the guard would pass on nothing",
);
assert(
	MODULE_CATALOG.length === 7,
	`expected seven catalog modules, got ${MODULE_CATALOG.length}`,
);

// Modules 3 to 6 each read one mart table. The mapping is fixed by the issue.
const EXPECTED_OUTPUT_TABLES: ReadonlyArray<readonly [number, string]> = [
	[3, "mart.issuer_supply_chain_exposure"],
	[4, "mart.issuer_analyst_ratings"],
	[5, "mart.fund_virtual_company"],
	[6, "mart.issuer_theme_purity"],
];
for (const [moduleNumber, table] of EXPECTED_OUTPUT_TABLES) {
	const entry = MODULE_CATALOG.find((candidate) => candidate.module === moduleNumber);
	assert(entry !== undefined, `module ${moduleNumber} is missing from the catalog`);
	assert(
		entry.outputTable === table,
		`module ${moduleNumber} must read ${table}, got ${String(entry.outputTable)}`,
	);
}

// Modules 1, 2 and 7 read the decision table, and no output table.
for (const entry of MODULE_CATALOG) {
	const decisionModule = [1, 2, 7].includes(entry.module);
	assert(
		decisionModule === (entry.outputTable === null),
		`module ${entry.module} has the wrong output table kind: ${String(entry.outputTable)}`,
	);
}

// Every catalog module maps to a table that the migrations create.
for (const entry of MODULE_CATALOG) {
	const table = moduleSourceTable(entry);
	assert(
		createsTable(table),
		`module ${entry.module} reads ${table}, which has no create table in db/migrations`,
	);
}

console.log("ok  every module catalog source table has a create table in db/migrations");
