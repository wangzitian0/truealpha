/**
 * #493: the inner-tree freeze — tools/route_manifest.json assigns service
 * PREFIXES only; this test pins the exact app-web route set to
 * routes.frozen.json so no route is added, removed, or moved without a
 * visible amendment to the frozen file in the same PR ("no second move").
 *
 * Discovered = every `page.tsx` / `route.ts` under src/app, mapped to its
 * URL path (dynamic segments kept as `[param]`). Assertions:
 *   1. every discovered route is listed in the frozen file;
 *   2. every `live: true` entry is discovered (nothing silently deleted);
 *   3. no `live: false` (planned) entry is discovered — building a planned
 *      page requires flipping it to live in the same PR;
 *   4. every route sits inside a prefix app-web owns in
 *      tools/route_manifest.json (keeps the two contracts consistent);
 *   5. every `live: true` /api/* route has a CONSUMER in src/ (#540).
 *
 * (5) exists because (1)-(4) prove the frozen file and the filesystem agree and
 * nothing more. `/api/auth/me` was frozen `live: true` by #493 while its only
 * caller — #368's client `AuthGuard`, superseded by #371's server-side gates —
 * was already dead, so a route with zero consumers became a checked-in contract
 * a later reader would treat as load-bearing. A supersession with no
 * decommission step is invisible to a consistency check; this makes it visible.
 *
 * Run standalone: `bun run tests/route-tree-freeze.test.ts`.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const APP_DIR = join(process.cwd(), "src", "app");

/** Every .ts/.tsx file under a directory, for the consumer scan in (5). */
function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    out.push(...(statSync(full).isDirectory() ? listSourceFiles(full) : /\.tsx?$/.test(entry) ? [full] : []));
  }
  return out;
}

function discoverRoutes(dir: string, prefix: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...discoverRoutes(full, `${prefix}/${entry}`));
    } else if (entry === "page.tsx" || entry === "route.ts") {
      out.push(prefix === "" ? "/" : prefix);
    }
  }
  return out;
}

const discovered = new Set(discoverRoutes(APP_DIR, ""));

const frozen = JSON.parse(readFileSync(join(process.cwd(), "routes.frozen.json"), "utf8")) as {
  routes: { path: string; live: boolean }[];
};
const live = new Set(frozen.routes.filter((r) => r.live).map((r) => r.path));
const planned = new Set(frozen.routes.filter((r) => !r.live).map((r) => r.path));

for (const route of discovered) {
  assert(
    live.has(route) || planned.has(route),
    `Route ${route} exists on disk but is not in routes.frozen.json — the #493 URL contract requires amending the frozen file in the same PR.`,
  );
  assert(
    !planned.has(route),
    `Route ${route} is marked planned (live: false) in routes.frozen.json but exists on disk — flip it to live in this PR.`,
  );
}
for (const route of live) {
  assert(
    discovered.has(route),
    `routes.frozen.json marks ${route} live but no page.tsx/route.ts exists — removals must amend the frozen file.`,
  );
}

const manifest = JSON.parse(
  readFileSync(join(process.cwd(), "..", "..", "tools", "route_manifest.json"), "utf8"),
) as { services: Record<string, { owns: string[]; root?: boolean }> };
const prefixes = manifest.services["app-web"].owns;
const rootOk = manifest.services["app-web"].root === true;
for (const route of frozen.routes.map((r) => r.path)) {
  const inPrefix = prefixes.some((p) => route === p || route.startsWith(`${p}/`));
  assert(
    inPrefix || (rootOk && route === "/"),
    `Route ${route} falls outside app-web's prefixes in tools/route_manifest.json (${prefixes.join(", ")}).`,
  );
}

// --- 5. a frozen live /api route must be reachable from the application ---
{
  const sourceFiles = listSourceFiles(join(process.cwd(), "src"));
  for (const route of frozen.routes) {
    if (!route.live || !route.path.startsWith("/api/")) continue;
    // Its own handler file does not count as a consumer.
    const handlerDir = join("src", "app", ...route.path.split("/").filter(Boolean));
    const consumers = sourceFiles.filter(
      (file) => !file.includes(handlerDir) && readFileSync(file, "utf8").includes(route.path),
    );
    assert(
      consumers.length > 0,
      `${route.path} is frozen \`live: true\` but nothing under src/ calls it. A route with no ` +
        `consumer is not a contract — either give it one or remove it from routes.frozen.json in ` +
        `the same PR (#540).`,
    );
  }
}

console.log(
  `#493 route-tree freeze passed: ${discovered.size} live routes match the contract, ${planned.size} planned pages pending.`,
);
