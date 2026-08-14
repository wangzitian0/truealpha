/**
 * #495 criterion 1: one read-state vocabulary, both route groups.
 *
 * The reopened issue's finding was structural, not cosmetic: the administrator
 * side had its own outcome unions (`OpsOverviewOutcome`, `QualityFunnelOutcome`)
 * and rendered them with per-page branches, so the discipline the research side
 * gets from one shared union and one shared renderer never reached it. Two
 * states ended up with no branch at all.
 *
 * Two static properties, same scan style as tests/route-group-boundary.test.ts
 * and tests/dashboard-boundary.test.ts:
 *
 *   1. No administrator READ module declares its own `kind: "ready"` union —
 *      read outcomes are `ReadState<T>` from `@/server/read-state`. Command
 *      outcomes (`src/server/admin/trigger.ts` returns accepted/invalid/…) are
 *      deliberately not covered: a write that was accepted is not a read state.
 *   2. Every `/admin` page that can render a non-`ready` outcome routes it
 *      through `ReadStateNotice`, rather than writing its own sentence inline.
 *
 * Run standalone: `bun run tests/admin-read-state-boundary.test.ts`.
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
    out.push(...(statSync(full).isDirectory() ? listFilesRecursive(full) : /\.tsx?$/.test(entry) ? [full] : []));
  }
  return out;
}

// --- 1. administrator read modules use the shared union ---
{
  const adminModules = listFilesRecursive(join(process.cwd(), "src/server/admin"));
  assert(adminModules.length > 0, "expected at least one module under src/server/admin to scan");
  for (const file of adminModules) {
    // A hand-rolled read union always spells its success case `kind: "ready"`.
    // Strip comments only — the string literals ARE the thing being matched.
    const raw = readFileSync(file, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    const declaresOwnReadUnion = /kind:\s*"ready"\s*;/.test(raw);
    assert(
      !declaresOwnReadUnion,
      `${file} declares its own read-outcome union (a \`kind: "ready";\` member). Administrator ` +
        `read surfaces must return \`ReadState<T>\` from @/server/read-state so they share the ` +
        `research side's states AND its words (#495).`,
    );
  }
}

// --- 2. /admin pages render absence through the shared component ---
{
  const adminPages = listFilesRecursive(join(process.cwd(), "src/app/admin")).filter((f) =>
    f.endsWith("page.tsx"),
  );
  assert(adminPages.length > 0, "expected at least one /admin page to scan");
  for (const file of adminPages) {
    const source = readFileSync(file, "utf8");
    // A page that never inspects a non-ready outcome has nothing to render a
    // notice for; one that does must use the shared renderer.
    const inspectsNonReady = /kind\s*!==\s*"ready"|kind\s*===\s*"denied"|kind\s*===\s*"error"/.test(source);
    if (!inspectsNonReady) continue;
    assert(
      source.includes("ReadStateNotice"),
      `${file} branches on a non-ready outcome but does not render <ReadStateNotice>. Absence ` +
        `states must go through the one renderer whose words are unit-tested (#495).`,
    );
  }
}

console.log("admin read-state boundary: one shared union, one shared renderer");
