/**
 * #371/#493: normal-user route group boundary — no file under
 * src/app/research may import an administrator server module. #493
 * generalized the rule from the single admin loader (which moved to
 * src/server/strategy-page.ts when strategy decisions became research
 * output) to the NAMING CONVENTION: every administrator-only server module
 * lives at `src/server/admin-*` or `src/server/admin/*`, and only
 * src/app/admin may import those paths. #495's ops read adapters must
 * follow the convention to stay covered by this scan. Static source scan,
 * same style as tests/dashboard-boundary.test.ts — it proves the import
 * graph.
 *
 * Run standalone: `bun run tests/route-group-boundary.test.ts`.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function listFilesRecursive(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) out.push(...listFilesRecursive(full));
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

// Matches `@/server/admin-<anything>` and `@/server/admin/<anything>` import
// specifiers (also their relative `server/admin...` spellings).
const ADMIN_IMPORT_PATTERN = /["'@\/.]server\/admin[-\/]/;

const researchFiles = listFilesRecursive(join(process.cwd(), "src/app/research"));
assert(researchFiles.length > 0, "expected at least one file under src/app/research to scan");
for (const file of researchFiles) {
  assert(
    !ADMIN_IMPORT_PATTERN.test(readFileSync(file, "utf8")),
    `${file} must not import an administrator server module (src/server/admin-*)`,
  );
}

// The shared component/lib layers research pages can reach are covered too.
for (const dir of ["src/components", "src/client"]) {
  for (const file of listFilesRecursive(join(process.cwd(), dir))) {
    assert(
      !ADMIN_IMPORT_PATTERN.test(readFileSync(file, "utf8")),
      `${file} must not import an administrator server module (src/server/admin-*)`,
    );
  }
}

// Guard against the rule going vacuous in the OTHER direction: the moved
// strategy loader must no longer match the admin convention (it is research
// surface now), and must be imported by the research strategy page.
statSync(join(process.cwd(), "src/server/strategy-page.ts"));
const strategyPage = readFileSync(join(process.cwd(), "src/app/research/strategy/page.tsx"), "utf8");
assert(
  strategyPage.includes("@/server/strategy-page"),
  "sanity check failed: /research/strategy must import the research-side strategy loader",
);

console.log("#371/#493 route-group boundary scan passed (research cannot import src/server/admin-*)");
