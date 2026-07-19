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

// #370: the whole point of this issue is that a deployed route must never fall back to
// the fixture. A behavioral test can't safely tell "hit real mart, got no rows" apart from
// "silently read the fixture" without depending on live database contents, so this checks
// the wiring statically instead: research-read.ts's bare constructor default must
// instantiate MartStrategyRunRepository, and FixtureStrategyRunRepository must not appear
// as a bare default anywhere in that file (it stays reachable only through explicit test
// injection, which callers pass as a constructor argument, not by relying on the module's
// default). This check is specific to that one file/class — read separately from the
// generic ADAPTER_PATHS loop above rather than reusing its loop-scoped `withoutBlockComments`,
// which after the loop holds whichever adapter path ran last, not necessarily this one.
{
  const researchReadSource = readFileSync(join(process.cwd(), "src/server/mart/research-read.ts"), "utf8");
  const withoutComments = researchReadSource.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
  assert(
    /repository\s*\?\?\s*new MartStrategyRunRepository\(\)/.test(withoutComments),
    "StrategyRunReadAdapter's bare default must be MartStrategyRunRepository, not the fixture",
  );
  assert(
    !/new FixtureStrategyRunRepository\(\)/.test(withoutComments),
    "the fixture repository must not appear as a bare default in the mart read adapter",
  );
}

console.log(`#370/#433 dashboard boundary scan passed (no cross-factor computation in ${ADAPTER_PATHS.length} mart adapters; mart-backed default)`);
