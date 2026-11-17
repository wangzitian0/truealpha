import { readdirSync, readFileSync } from "node:fs";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const MIGRATIONS_DIR = new URL("../../../db/migrations/", import.meta.url);
const migrationSql = readdirSync(MIGRATIONS_DIR)
  .filter((name) => name.endsWith(".sql"))
  .sort()
  .map((name) => readFileSync(new URL(name, MIGRATIONS_DIR), "utf8").replace(/--[^\n]*/g, " "))
  .join("\n");

const svcSource = readFileSync(
  new URL("../../../apps/llm-service/src/llm_service/services/research_service.py", import.meta.url),
  "utf8",
);
const catalogMatch = svcSource.match(/MODULE_CATALOG:.*?=\s*\(([\s\S]*?)\n\)/);
assert(catalogMatch !== null, "research_service.py must declare MODULE_CATALOG");
const outputTables = [...catalogMatch[1].matchAll(/"output_table":\s*"([^"]+)"/g)].map((m) => m[1]);
assert(outputTables.length === 4, `expected 4 external output tables in MODULE_CATALOG, got ${outputTables.length}`);

for (const table of outputTables) {
  const escaped = table.replace(/\./g, "\\.");
  assert(
    new RegExp(`create\\s+table\\s+(if\\s+not\\s+exists\\s+)?${escaped}\\s*\\(`, "i").test(migrationSql),
    `catalog table ${table} missing from db/migrations`,
  );
}
console.log("ok  every module catalog source table has a create table in db/migrations");
