/**
 * #1716 / #1748: Zero-database frontend, single OpenAPI 3.1 contract ratchet, and active UI caller gate.
 */

import { execFileSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const APP_ROOT = new URL("../", import.meta.url).pathname;
const openapi = JSON.parse(readFileSync(join(APP_ROOT, "openapi.json"), "utf8")) as {
  paths?: Record<string, Record<string, { operationId?: string }>>;
};
const paths = Object.keys(openapi.paths ?? {});
assert(paths.length > 0, "expected OpenAPI paths to be non-empty");

const opIds = new Set<string>();
for (const [path, methods] of Object.entries(openapi.paths ?? {})) {
  for (const [method, op] of Object.entries(methods)) {
    const id = op.operationId ?? "";
    assert(id.length > 0, `missing operationId on ${method.toUpperCase()} ${path}`);
    assert(!opIds.has(id), `duplicate operationId: ${id}`);
    opIds.add(id);
  }
}
assert(opIds.size > 0, "expected non-empty OpenAPI HTTP operations");

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

for (const p of paths) {
  assert(generatedTs.includes(`"${p}"`), `openapi.generated.ts missing path ${p}`);
}

const pkg = JSON.parse(readFileSync(join(APP_ROOT, "package.json"), "utf8")) as {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
};
const allDeps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
for (const forbidden of ["pg", "@types/pg", "@aws-sdk/client-s3", "bcryptjs", "@types/bcryptjs", "jose"]) {
  assert(!(forbidden in allDeps), `forbidden DB/auth package ${forbidden} in package.json`);
}

console.log(`apiSurfaceRatchet: ${paths.length} OpenAPI paths (${opIds.size} ops) and zero-DB dependencies verified`);
