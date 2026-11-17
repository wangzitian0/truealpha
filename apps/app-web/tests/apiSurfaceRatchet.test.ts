/**
 * #1716: Zero-database frontend and single OpenAPI 3.1 contract ratchet.
 */

import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function walkFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkFiles(full));
    else if (/\.(ts|tsx|mjs|js)$/.test(entry.name)) out.push(full);
  }
  return out;
}

const APP_ROOT = new URL("../", import.meta.url).pathname;
const openapi = JSON.parse(readFileSync(join(APP_ROOT, "openapi.json"), "utf8")) as {
  paths?: Record<string, Record<string, { operationId?: string }>>;
};
const paths = Object.keys(openapi.paths ?? {});
assert(paths.length >= 21, `expected >= 21 OpenAPI paths, found ${paths.length}`);

const opIds = new Set<string>();
for (const [path, methods] of Object.entries(openapi.paths ?? {})) {
  for (const [method, op] of Object.entries(methods)) {
    const id = op.operationId ?? "";
    assert(id.length > 0, `missing operationId on ${method.toUpperCase()} ${path}`);
    assert(!opIds.has(id), `duplicate operationId: ${id}`);
    opIds.add(id);
  }
}

const generatedTs = readFileSync(join(APP_ROOT, "src/client/openapi.generated.ts"), "utf8");
const tmpOut = join(tmpdir(), `openapi-ratchet-${process.pid}.ts`);
try {
  execFileSync(join(APP_ROOT, "node_modules/.bin/openapi-typescript"), [join(APP_ROOT, "openapi.json"), "-o", tmpOut], { stdio: "pipe" });
  const normalized = "// Auto-generated from openapi.json\n" + readFileSync(tmpOut, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").split(/\r?\n/).map((l) => l.trim())
    .filter((l) => l && !/^[a-zA-Z0-9_?]+:\s*never;?$/.test(l) && !/^parameters:\s*\{\s*\};?$/.test(l))
    .join(" ").replace(/\s*([{};:,])\s*/g, "$1")
    .replace(/export(interface|type)/g, "\nexport $1").replace(/("\/[^"]+":\{)/g, "\n$1").trim() + "\n";
  assert(normalized === generatedTs, "src/client/openapi.generated.ts drifted from openapi.json; run bun run generate:api");
} finally {
  rmSync(tmpOut, { force: true });
}
const apiClientTs = readFileSync(join(APP_ROOT, "src/client/api.ts"), "utf8");
const ALLOW_NO_UI_CALLER = new Set(["/health", "/livez", "/readyz", "/api/v1/health"]);

for (const p of paths) {
  assert(generatedTs.includes(`"${p}"`), `openapi.generated.ts missing path ${p}`);
  if (!ALLOW_NO_UI_CALLER.has(p)) {
    const escaped = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\{[^}]+\\\}/g, "\\$\\{[^}]+\\}");
    assert(
      apiClientTs.includes(p) || new RegExp(escaped).test(apiClientTs),
      `src/client/api.ts has no caller for OpenAPI path ${p}`,
    );
  }
}

const pkg = JSON.parse(readFileSync(join(APP_ROOT, "package.json"), "utf8")) as {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
};
const allDeps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
for (const forbidden of ["pg", "@types/pg", "@aws-sdk/client-s3", "bcryptjs", "@types/bcryptjs", "jose"]) {
  assert(!(forbidden in allDeps), `forbidden DB/auth package ${forbidden} in package.json`);
}

for (const file of walkFiles(join(APP_ROOT, "src"))) {
  const content = readFileSync(file, "utf8");
  assert(
    !/from\s+["'](?:pg|bcryptjs|jose|@aws-sdk\/client-s3)["']/.test(content),
    `${file} imports forbidden direct-DB/storage package`,
  );
  assert(
    !/\b(createPool|new Pool|mart_readonly|mart_admin|DATABASE_URL|PGPASSWORD)\b/.test(content),
    `${file} references direct database credentials or pool`,
  );
}

console.log(`apiSurfaceRatchet: ${paths.length} OpenAPI paths (${opIds.size} ops) and zero-DB frontend verified`);
