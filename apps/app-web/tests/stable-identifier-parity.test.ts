/**
 * #1010: documents.ts's STABLE_ID_PATTERN said it "mirrors truealpha_contracts.access.
 * _stable_coordinate exactly" and nothing checked that. TypeScript cannot import the Python
 * constant directly, so this measures the two against each other through the one artifact both
 * sides already trust: the conformance bundle `export_issue58.py --check` keeps in sync with the
 * Python contracts in CI. `CaptureManifest.partition_key` is declared with the shared
 * `truealpha_contracts.common.STABLE_ID_PATTERN` (see #1010), so its emitted JSON Schema `pattern`
 * is that grammar, in Python's own regex spelling.
 *
 * Comparing regex SOURCE TEXT would fail on any harmless reordering or escaping difference
 * between Python's and JavaScript's regex dialects. Comparing BEHAVIOR — what each pattern
 * accepts, character by character — is what actually has to agree, and it is what
 * test_stable_identifier_is_shared.py measures on the Python side.
 *
 * Run standalone: `bun run tests/stable-identifier-parity.test.ts`.
 */

import { readFileSync } from "node:fs";

import { STABLE_ID_PATTERN } from "../src/server/documents";

const schemaUrl = new URL("../../../libs/contracts/conformance/issue58.schemas.json", import.meta.url);

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function readJson(url: URL): unknown {
  return JSON.parse(readFileSync(url, "utf8")) as unknown;
}

function object(value: unknown, label: string): Record<string, unknown> {
  assert(typeof value === "object" && value !== null && !Array.isArray(value), `${label} must be an object`);
  return value as Record<string, unknown>;
}

// Every printable ASCII character plus three non-ASCII probes, each tried as the sole character
// and as the second character (after a leading "a", which both patterns must accept alone). For a
// pattern of the shape `^[first][rest]*$` — which is what both sides declare — agreeing on every
// one of these means the two patterns accept exactly the same set of strings.
const PRINTABLE_ASCII = Array.from({ length: 0x7f - 0x20 }, (_, i) => String.fromCharCode(0x20 + i));
const PROBE_CHARACTERS = [...PRINTABLE_ASCII, "é", "Ａ", "٣"];

function run(): void {
  const schemaBundle = object(readJson(schemaUrl), "$.schema_bundle");
  const captureManifestSchema = object(schemaBundle.schemas, "$.schemas")["CaptureManifest"];
  const partitionKeySchema = object(object(captureManifestSchema, "$.schemas.CaptureManifest").properties, "$.properties")[
    "partition_key"
  ];
  const pythonPattern = object(partitionKeySchema, "$.properties.partition_key").pattern;
  assert(typeof pythonPattern === "string", "CaptureManifest.partition_key must declare a string pattern");

  const pythonRegex = new RegExp(pythonPattern);

  assert(pythonRegex.test("a"), "sanity: the Python pattern must accept a bare stable identifier");
  assert(STABLE_ID_PATTERN.test("a"), "sanity: the TS pattern must accept a bare stable identifier");

  const disagreements: string[] = [];
  for (const character of PROBE_CHARACTERS) {
    for (const probe of [character, `a${character}`]) {
      if (pythonRegex.test(probe) !== STABLE_ID_PATTERN.test(probe)) {
        disagreements.push(JSON.stringify(probe));
      }
    }
  }
  assert(
    disagreements.length === 0,
    `documents.ts's STABLE_ID_PATTERN disagrees with the Python grammar (source: ${pythonPattern}) on: ${disagreements.join(", ")}`,
  );

  // The symptom #1010 found: partition keys real DataRequirement/PlannedDemandCell records use,
  // several of them uppercase, must all be accepted by both patterns identically.
  for (const partitionKey of ["2025-fy", "fy2025", "accession:2", "2025FY", "2026FY", "2026Q2", "FY2025"]) {
    assert(
      pythonRegex.test(partitionKey) === STABLE_ID_PATTERN.test(partitionKey),
      `partition key ${JSON.stringify(partitionKey)} disagrees between the Python and TS patterns`,
    );
  }

  console.log("stable-identifier-parity.test.ts: all assertions passed");
}

run();
