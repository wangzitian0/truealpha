/**
 * #1468: the standalone output that the Docker image copies must not hold project files.
 *
 * Next 16.4.2 with Turbopack wrote `src`, `scripts`, `bun.lock` and the configs into
 * `.next/standalone`, because one `readFileSync` with a computed path in `src/contracts/strategyRun.ts`
 * made the file tracer include the whole project. `e2e/walk-tree.mjs` calls
 * `unexpectedStandaloneEntries` on the CI build. This proves the function on directories that look
 * like the Next 15.5 output, the Next 16 output with the defect, and no build at all.
 *
 * Run standalone: `bun run tests/standalone-shape.test.ts`.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as shape from "../e2e/standalone-shape.mjs";

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const root = mkdtempSync(join(tmpdir(), "standalone-shape-"));
function build(name: string, entries: string[]): string {
	const dir = join(root, name);
	mkdirSync(dir);
	for (const entry of entries) {
		if (entry === ".next" || entry === "node_modules" || entry === "src" || entry === "e2e") mkdirSync(join(dir, entry));
		else writeFileSync(join(dir, entry), "x");
	}
	return dir;
}

try {
	const healthy = build("healthy", [".next", "node_modules", "package.json", "server.js"]);
	assert(JSON.stringify(shape.unexpectedStandaloneEntries(healthy)) === "[]", "the Next 15.5 shape has no unexpected entry");

	const traced = build("traced", [".next", "node_modules", "package.json", "server.js", "src", "bun.lock", "next.config.mjs", "e2e"]);
	assert(
		JSON.stringify(shape.unexpectedStandaloneEntries(traced)) === JSON.stringify(["bun.lock", "e2e", "next.config.mjs", "src"]),
		`the traced project files must be named: ${JSON.stringify(shape.unexpectedStandaloneEntries(traced))}`,
	);

	// No server.js means no standalone build: the walk against a dev server has nothing to check.
	const none = build("none", ["src"]);
	assert(shape.unexpectedStandaloneEntries(none) === null, "a directory without server.js is not a standalone build");
	assert(shape.unexpectedStandaloneEntries(join(root, "missing")) === null, "a missing directory is not a standalone build");
} finally {
	rmSync(root, { recursive: true, force: true });
}

console.log("#1468 standalone shape passed");
