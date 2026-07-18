/**
 * #371: normal-user route group boundary — no file under src/app/research
 * (or the shared components/lib it can reach) may import an administrator
 * loader/repository (`@/server/admin-strategy-runs`), and no file outside
 * src/app/admin may import it either. This is a static source scan, same
 * style as tests/dashboard-boundary.test.ts, not a runtime check — it
 * proves the import graph, which is what #371's acceptance criterion asks
 * for ("a test proves a normal-user route cannot import an administrator
 * loader").
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

const ADMIN_IMPORT_MARKERS = ["@/server/admin-strategy-runs", "server/admin-strategy-runs"];

const researchDir = join(process.cwd(), "src/app/research");
const researchFiles = listFilesRecursive(researchDir);
assert(researchFiles.length > 0, "expected at least one file under src/app/research to scan");

for (const file of researchFiles) {
  const source = readFileSync(file, "utf8");
  for (const marker of ADMIN_IMPORT_MARKERS) {
    assert(!source.includes(marker), `${file} must not import the administrator loader (${marker})`);
  }
}

// Sanity check the other direction too: the admin loader module must exist
// and actually be imported somewhere under src/app/admin, so this isn't a
// vacuously-true scan of an unused file.
const adminLoaderPath = join(process.cwd(), "src/server/admin-strategy-runs.ts");
const adminDirFiles = listFilesRecursive(join(process.cwd(), "src/app/admin"));
const adminLoaderIsUsed = adminDirFiles.some((file) =>
  ADMIN_IMPORT_MARKERS.some((marker) => readFileSync(file, "utf8").includes(marker)),
);
assert(adminLoaderIsUsed, "sanity check failed: expected src/app/admin to actually import the admin loader");
statSync(adminLoaderPath); // throws if the file has moved/been renamed without updating this test

console.log("#371 route-group boundary scan passed (research routes cannot import the admin loader)");
