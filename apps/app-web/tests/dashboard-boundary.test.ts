/**
 * #370/#433 boundary lint: the App does deterministic reformatting only.
 *
 * Every mart read adapter listed in `ADAPTER_PATHS` may sort, filter, paginate, label,
 * and copy already-materialized values through byte-exact. It must never join two
 * factors or two time points into a new metric in the Next.js backend (init.md Section
 * 1, rule 2). This statically forbids numeric computation in each adapter: no
 * arithmetic operators and no numeric/aggregation primitive. Metric arithmetic belongs
 * in `libs/factors` -> `mart`.
 *
 * Index arithmetic for pagination lives in the separate `pagination.ts` module, which this
 * scan deliberately does not target — page offsets are not metrics.
 *
 * Run standalone: `bun run tests/dashboard-boundary.test.ts`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const ADAPTER_PATHS = ["src/server/mart/research-read.ts", "src/server/mart/topt-gppe-repository.ts"];

for (const relativePath of ADAPTER_PATHS) {
  const rawSource = readFileSync(join(process.cwd(), relativePath), "utf8");

  // Strip block comments, then string/template literals, then line comments, so the scan
  // sees only executable code (comments and paths contain '/' and '*' legitimately).
  const withoutBlockComments = rawSource.replace(/\/\*[\s\S]*?\*\//g, " ");
  const withoutStrings = withoutBlockComments
    .replace(/`[^`]*`/g, " ")
    .replace(/"[^"]*"/g, " ")
    .replace(/'[^']*'/g, " ");
  const code = withoutStrings.replace(/\/\/[^\n]*/g, " ");

  // Arithmetic operators that would combine values into a new metric. '+' and '-' are
  // intentionally allowed (string joins, negatives, and there is no metric arithmetic here);
  // multiplication, division, and modulo have no legitimate reformatting use on a metric.
  const FORBIDDEN_OPERATORS: readonly [RegExp, string][] = [
    [/\*\*/, "exponentiation '**'"],
    [/[^*]\*[^*/]/, "multiplication '*'"],
    [/[^/*]\/[^/*]/, "division '/'"],
    [/%/, "modulo '%'"],
  ];

  for (const [pattern, label] of FORBIDDEN_OPERATORS) {
    assert(!pattern.test(code), `${relativePath} must not contain ${label} (cross-factor computation)`);
  }

  const FORBIDDEN_CALLS: readonly string[] = ["Math.", "Number(", "parseFloat", "parseInt", ".reduce(", "BigInt("];
  for (const token of FORBIDDEN_CALLS) {
    assert(!code.includes(token), `${relativePath} must not call ${token} (numeric computation)`);
  }

  // The adapter must not pull in a decimal/number computation dependency.
  assert(!/from\s+["'][^"']*decimal/i.test(rawSource), `${relativePath} must not import a decimal library`);
}

console.log(`#370/#433 dashboard boundary scan passed (no cross-factor computation in ${ADAPTER_PATHS.length} mart adapters)`);

// --- #370 appended acceptance / #429 P3: the /research loaders' bare default is the
// mart-backed adapter; the fixture stays reachable only through explicit test injection.
// A live-DB behavioral test cannot distinguish "real mart, legitimately empty" from
// "silently fell back to the fixture" without depending on ambient database content,
// so the wiring is checked statically instead. ---
{
  const loaders = readFileSync(join(process.cwd(), "src/server/dashboard.ts"), "utf8");
  assert(
    loaders.includes("adapter: MartAdapterLike = new MartResearchReadAdapter()"),
    "dashboard.ts loaders must default to the mart-backed adapter",
  );
  assert(
    !loaders.includes("FixtureMartReadAdapter") && !loaders.includes("FixtureStrategyRunRepository"),
    "dashboard.ts must not reference the fixture adapter/repository (tests only)",
  );

  const adapterSource = readFileSync(join(process.cwd(), "src/server/mart/research-read.ts"), "utf8");
  assert(
    adapterSource.includes("this.repository = repository ?? new MartStrategyRunRepository()"),
    "MartResearchReadAdapter's bare default must be MartStrategyRunRepository (mart_readonly)",
  );

  console.log("#370 default wiring scan passed (/research loaders default to the mart-backed adapter)");
}
