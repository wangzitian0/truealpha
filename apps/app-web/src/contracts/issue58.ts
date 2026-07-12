const CONTRACT_SET = "truealpha.issue-58.exact-contracts.v1";
const CONTRACT_NAMES = ["CaptureScope", "CaptureManifest", "SnapshotManifest", "ReleaseManifest"] as const;
const MUTABLE_VERSION_ALIASES = new Set(["latest", "current", "default", "stable", "main", "head"]);

export type ContractName = (typeof CONTRACT_NAMES)[number];
export type JsonPrimitive = null | boolean | number | string;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export interface Issue58SchemaBundle {
  contract_set: string;
  schema_sha256: Record<ContractName, string>;
  schemas: Record<ContractName, JsonObject>;
}

export interface Issue58FixtureBundle {
  contract_set: string;
  schema_sha256: Record<ContractName, string>;
  contracts: Record<ContractName, JsonObject>;
}

export class ContractValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractValidationError";
  }
}

function fail(path: string, message: string): never {
  throw new ContractValidationError(`${path}: ${message}`);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asObject(value: unknown, path: string): Record<string, unknown> {
  if (!isObject(value)) {
    fail(path, "expected an object");
  }
  return value;
}

function asString(value: unknown, path: string): string {
  if (typeof value !== "string") {
    fail(path, "expected a string");
  }
  return value;
}

function assertExactKeys(value: Record<string, unknown>, expected: readonly string[], path: string): void {
  const expectedSet = new Set(expected);
  const unknown = Object.keys(value).filter((key) => !expectedSet.has(key));
  const missing = expected.filter((key) => !(key in value));
  if (unknown.length > 0) {
    fail(path, `unknown fields: ${unknown.sort().join(", ")}`);
  }
  if (missing.length > 0) {
    fail(path, `missing fields: ${missing.sort().join(", ")}`);
  }
}

function hasMutableVersionToken(value: string): boolean {
  return value
    .toLowerCase()
    .split(/[._:/@+\-]/)
    .some((token) => MUTABLE_VERSION_ALIASES.has(token));
}

function resolveReference(reference: string, root: Record<string, unknown>, path: string): Record<string, unknown> {
  if (!reference.startsWith("#/")) {
    fail(path, `unsupported non-local schema reference ${reference}`);
  }
  let current: unknown = root;
  for (const rawToken of reference.slice(2).split("/")) {
    const token = rawToken.replaceAll("~1", "/").replaceAll("~0", "~");
    current = asObject(current, path)[token];
  }
  return asObject(current, path);
}

function valuesEqual(left: unknown, right: unknown): boolean {
  return canonicalJson(left as JsonValue) === canonicalJson(right as JsonValue);
}

function validateFormat(format: string, value: string, path: string): void {
  if (format === "date") {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      fail(path, "expected an ISO calendar date");
    }
    return;
  }
  if (format === "date-time") {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
      fail(path, "expected an aware ISO date-time");
    }
    return;
  }
  if (format === "duration" && !/^P(?=\d|T\d)(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$/.test(value)) {
    fail(path, "expected a positive ISO-8601 duration");
  }
}

function validateSchemaNode(
  schemaValue: unknown,
  value: unknown,
  root: Record<string, unknown>,
  path: string,
): void {
  const schema = asObject(schemaValue, `${path} schema`);
  if (typeof schema.$ref === "string") {
    validateSchemaNode(resolveReference(schema.$ref, root, path), value, root, path);
    return;
  }

  if (Array.isArray(schema.anyOf)) {
    const failures: string[] = [];
    for (const branch of schema.anyOf) {
      try {
        validateSchemaNode(branch, value, root, path);
        return;
      } catch (error) {
        failures.push(error instanceof Error ? error.message : String(error));
      }
    }
    fail(path, `does not match any allowed shape (${failures.join("; ")})`);
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((candidate) => valuesEqual(candidate, value))) {
    fail(path, "value is outside the declared enum");
  }

  const schemaType = schema.type;
  if (schemaType === "null") {
    if (value !== null) fail(path, "expected null");
    return;
  }
  if (schemaType === "boolean") {
    if (typeof value !== "boolean") fail(path, "expected a boolean");
    return;
  }
  if (schemaType === "integer") {
    if (typeof value !== "number" || !Number.isSafeInteger(value)) fail(path, "expected a safe integer");
  } else if (schemaType === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) fail(path, "expected a finite number");
  } else if (schemaType === "string") {
    if (typeof value !== "string") fail(path, "expected a string");
    if (typeof schema.minLength === "number" && value.length < schema.minLength) {
      fail(path, `string is shorter than ${schema.minLength}`);
    }
    if (typeof schema.pattern === "string" && !new RegExp(schema.pattern).test(value)) {
      fail(path, `string does not match ${schema.pattern}`);
    }
    if (typeof schema.format === "string") validateFormat(schema.format, value, path);
    return;
  } else if (schemaType === "array") {
    if (!Array.isArray(value)) fail(path, "expected an array");
    if (typeof schema.minItems === "number" && value.length < schema.minItems) {
      fail(path, `array has fewer than ${schema.minItems} items`);
    }
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
      fail(path, `array has more than ${schema.maxItems} items`);
    }
    if (schema.items !== undefined) {
      value.forEach((item, index) => validateSchemaNode(schema.items, item, root, `${path}[${index}]`));
    }
    return;
  } else if (schemaType === "object") {
    const object = asObject(value, path);
    const properties = schema.properties === undefined ? {} : asObject(schema.properties, `${path} properties`);
    const required = Array.isArray(schema.required) ? schema.required.map(String) : [];
    for (const key of required) {
      if (!(key in object)) fail(path, `missing required field ${key}`);
    }
    for (const [key, child] of Object.entries(object)) {
      if (key in properties) {
        validateSchemaNode(properties[key], child, root, `${path}.${key}`);
      } else if (schema.additionalProperties === false) {
        fail(path, `unknown field ${key}`);
      } else if (isObject(schema.additionalProperties)) {
        validateSchemaNode(schema.additionalProperties, child, root, `${path}.${key}`);
      }
    }
    return;
  }

  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) fail(path, "number is below minimum");
    if (typeof schema.maximum === "number" && value > schema.maximum) fail(path, "number is above maximum");
  }
}

export function validateJsonSchema(schema: JsonObject, value: unknown, path = "$"): void {
  validateSchemaNode(schema, value, schema, path);
}

function quotePythonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) => {
    return `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`;
  });
}

export function canonicalJson(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ContractValidationError("canonical JSON forbids non-finite numbers");
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (typeof value === "string") return quotePythonString(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const entries = Object.entries(value).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries.map(([key, child]) => `${quotePythonString(key)}:${canonicalJson(child)}`).join(",")}}`;
}

export async function canonicalSha256(value: JsonValue): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function copyWithout(value: JsonObject, excluded: readonly string[]): JsonObject {
  const result: JsonObject = {};
  const excludedSet = new Set(excluded);
  for (const [key, child] of Object.entries(value)) {
    if (!excludedSet.has(key)) result[key] = child;
  }
  return result;
}

async function assertIdentity(
  value: JsonObject,
  idField: string,
  hashField: string,
  prefix: string,
  excluded: readonly string[] = [],
): Promise<void> {
  const id = asString(value[idField], `$.${idField}`);
  const suppliedHash = asString(value[hashField], `$.${hashField}`);
  const calculatedHash = await canonicalSha256(copyWithout(value, [idField, hashField, ...excluded]));
  if (suppliedHash !== calculatedHash) {
    fail(`$.${hashField}`, `content hash mismatch: expected ${calculatedHash}`);
  }
  if (id !== `${prefix}:${calculatedHash}`) {
    fail(`$.${idField}`, `content ID mismatch for ${prefix}`);
  }
}

function assertReferencePair(value: JsonObject, idField: string, hashField: string, requiredSuffix: boolean): void {
  const id = asString(value[idField], `$.${idField}`);
  const digest = asString(value[hashField], `$.${hashField}`);
  const suffix = id.slice(id.lastIndexOf(":") + 1);
  if ((requiredSuffix || /^[0-9a-f]{64}$/.test(suffix)) && suffix !== digest) {
    fail(`$.${idField}`, `does not bind $.${hashField}`);
  }
}

function nestedObject(value: JsonObject, key: string, path: string): JsonObject {
  return asObject(value[key], path) as JsonObject;
}

function objectArray(value: JsonObject, key: string, path: string): JsonObject[] {
  const items = value[key];
  if (!Array.isArray(items) || items.some((item) => !isObject(item))) fail(path, "expected an object array");
  return items as JsonObject[];
}

async function validateCaptureIdentities(scope: JsonObject, manifest: JsonObject): Promise<void> {
  await assertIdentity(scope, "capture_scope_id", "content_sha256", "capture-scope");
  const requirements = objectArray(scope, "requirements", "$.CaptureScope.requirements");
  const requirementById = new Map(
    requirements.map((requirement) => [requirement.capture_requirement_id, requirement]),
  );
  for (const requirement of requirements) {
    await assertIdentity(requirement, "capture_requirement_id", "content_sha256", "capture-requirement");
  }
  await assertIdentity(manifest, "capture_manifest_id", "content_sha256", "capture-manifest");
  for (const cell of objectArray(manifest, "cells", "$.CaptureManifest.cells")) {
    const requirement = requirementById.get(cell.capture_requirement_id);
    if (requirement === undefined) {
      fail("$.CaptureManifest.cells[].capture_requirement_id", "unknown CaptureRequirement");
    }
    const evidenceRows = objectArray(cell, "evidence", "$.CaptureManifest.cells[].evidence");
    if (cell.status === "complete" && evidenceRows.length === 0) {
      fail("$.CaptureManifest.cells[].evidence", "complete cell is missing required evidence");
    }
    for (const evidence of evidenceRows) {
      await assertIdentity(evidence, "evidence_id", "content_sha256", "capture-evidence");
      if (
        evidence.semantic_type_id !== requirement.semantic_type_id ||
        evidence.semantic_type_version !== requirement.semantic_type_version
      ) {
        fail("$.CaptureManifest.cells[].evidence[].semantic_type_id", "CaptureRequirement semantic type mismatch");
      }
      const populatedFields = evidence.populated_fields;
      const requiredFields = requirement.required_fields;
      if (!Array.isArray(populatedFields) || !Array.isArray(requiredFields)) {
        fail("$.CaptureManifest.cells[].evidence[].populated_fields", "missing required populated-fields evidence");
      }
      const populated = new Set(populatedFields);
      const missingFields = requiredFields.filter((field) => !populated.has(field));
      if (missingFields.length > 0) {
        fail(
          "$.CaptureManifest.cells[].evidence[].populated_fields",
          `missing required fields: ${missingFields.join(", ")}`,
        );
      }
      if (evidence.quality_status !== "pass") {
        fail("$.CaptureManifest.cells[].evidence[].quality_status", "quality status is not pass");
      }
    }
    await assertIdentity(cell, "capture_cell_id", "content_sha256", "capture-cell");
  }
  for (const [idField, hashField] of [
    ["research_catalog_id", "research_catalog_sha256"],
    ["applicability_catalog_id", "applicability_catalog_sha256"],
    ["source_coverage_catalog_id", "source_coverage_catalog_sha256"],
    ["slo_catalog_id", "slo_catalog_sha256"],
    ["source_registry_id", "source_registry_sha256"],
    ["semantic_type_registry_id", "semantic_type_registry_sha256"],
  ]) {
    assertReferencePair(scope, idField, hashField, false);
    assertReferencePair(manifest, idField, hashField, false);
  }
}

async function validateSnapshotIdentity(snapshot: JsonObject): Promise<void> {
  await assertIdentity(snapshot, "snapshot_id", "content_sha256", "snapshot");
  const request = nestedObject(snapshot, "request", "$.SnapshotManifest.request");
  await assertIdentity(request, "snapshot_request_id", "content_sha256", "snapshot-request");
  for (const record of objectArray(snapshot, "normalized_records", "$.SnapshotManifest.normalized_records")) {
    const draft = nestedObject(record, "draft", "$.SnapshotManifest.normalized_records[].draft");
    await assertIdentity(draft, "semantic_draft_id", "content_sha256", "semantic-draft");
    await assertIdentity(record, "normalized_record_id", "content_sha256", "normalized-record");
  }
  for (const demand of objectArray(request, "demand_cells", "$.SnapshotManifest.request.demand_cells")) {
    const plannedCellPayload: JsonObject = {
      requirement_id: demand.requirement_id as JsonValue,
      capture_requirement_id: demand.capture_requirement_id as JsonValue,
      semantic_type_id: demand.semantic_type_id as JsonValue,
      domain: demand.domain as JsonValue,
      subject: demand.subject as JsonValue,
      partition_key: demand.partition_key as JsonValue,
    };
    const expectedId = `planned-demand-cell:${await canonicalSha256(plannedCellPayload)}`;
    if (demand.planned_cell_id !== expectedId) {
      fail("$.SnapshotManifest.request.demand_cells[].planned_cell_id", "frozen demand identity mismatch");
    }
  }
}

async function validateReleaseIdentity(release: JsonObject): Promise<void> {
  await assertIdentity(
    release,
    "release_manifest_id",
    "manifest_sha256",
    "release-manifest",
    ["manifest_signature_ref"],
  );
  for (const [idField, hashField] of [
    ["research_catalog_id", "research_catalog_sha256"],
    ["capture_scope_id", "capture_scope_sha256"],
    ["applicability_catalog_id", "applicability_catalog_sha256"],
    ["source_coverage_catalog_id", "source_coverage_catalog_sha256"],
    ["source_readiness_report_id", "source_readiness_report_sha256"],
    ["slo_catalog_id", "slo_catalog_sha256"],
    ["consumer_slo_catalog_id", "consumer_slo_catalog_sha256"],
    ["usage_telemetry_slo_catalog_id", "usage_telemetry_slo_catalog_sha256"],
    ["registry_snapshot_id", "registry_snapshot_sha256"],
    ["source_registry_id", "source_registry_sha256"],
    ["semantic_type_registry_id", "semantic_type_registry_sha256"],
    ["identifier_type_registry_id", "identifier_type_registry_sha256"],
  ]) {
    assertReferencePair(release, idField, hashField, true);
  }
  const approvedModels = objectArray(
    release,
    "approved_model_revisions",
    "$.ReleaseManifest.approved_model_revisions",
  );
  const approvedModelHashes = new Map<string, unknown>();
  for (const modelRevision of approvedModels) {
    await assertIdentity(modelRevision, "model_revision_id", "content_sha256", "model-revision");
    if (hasMutableVersionToken(asString(modelRevision.immutable_revision, "$.immutable_revision"))) {
      fail("$.ReleaseManifest.approved_model_revisions[].immutable_revision", "mutable model revision");
    }
    approvedModelHashes.set(asString(modelRevision.model_revision_id, "$.model_revision_id"), modelRevision.content_sha256);
  }
  for (const template of objectArray(
    release,
    "approved_extraction_templates",
    "$.ReleaseManifest.approved_extraction_templates",
  )) {
    await assertIdentity(template, "extraction_template_id", "content_sha256", "extraction-template");
    if (hasMutableVersionToken(asString(template.template_version, "$.template_version"))) {
      fail("$.ReleaseManifest.approved_extraction_templates[].template_version", "mutable extraction template version");
    }
    assertReferencePair(template, "model_revision_id", "model_revision_sha256", true);
    if (approvedModelHashes.get(asString(template.model_revision_id, "$.model_revision_id")) !== template.model_revision_sha256) {
      fail("$.ReleaseManifest.approved_extraction_templates[].model_revision_id", "unapproved model revision");
    }
  }
}

function assertSame(left: unknown, right: unknown, path: string): void {
  if (!valuesEqual(left, right)) fail(path, "cross-contract binding mismatch");
}

function assertSnapshotExtractionBindings(snapshot: JsonObject, release: JsonObject): void {
  const approvedModels = new Map(
    objectArray(release, "approved_model_revisions", "$.ReleaseManifest.approved_model_revisions").map((model) => [
      model.model_revision_id,
      model,
    ]),
  );
  const approvedTemplates = new Map(
    objectArray(
      release,
      "approved_extraction_templates",
      "$.ReleaseManifest.approved_extraction_templates",
    ).map((template) => [template.extraction_template_id, template]),
  );
  const extractionFields = [
    "model_revision_id",
    "model_revision_sha256",
    "extraction_template_id",
    "extraction_template_sha256",
    "extraction_invocation_id",
    "extraction_invocation_sha256",
  ] as const;
  for (const record of objectArray(snapshot, "normalized_records", "$.SnapshotManifest.normalized_records")) {
    const draft = nestedObject(record, "draft", "$.SnapshotManifest.normalized_records[].draft");
    if (draft.producer_kind === "deterministic_normalizer") {
      if (extractionFields.some((field) => draft[field] !== null)) {
        fail("$.SnapshotManifest.normalized_records[].draft", "deterministic normalizer carries extraction bindings");
      }
      continue;
    }
    if (draft.producer_kind !== "versioned_extraction") {
      fail("$.SnapshotManifest.normalized_records[].draft.producer_kind", "unknown semantic producer kind");
    }
    if (extractionFields.some((field) => typeof draft[field] !== "string")) {
      fail("$.SnapshotManifest.normalized_records[].draft", "versioned extraction binding is incomplete");
    }
    assertReferencePair(draft, "model_revision_id", "model_revision_sha256", true);
    assertReferencePair(draft, "extraction_template_id", "extraction_template_sha256", true);
    assertReferencePair(draft, "extraction_invocation_id", "extraction_invocation_sha256", true);
    const model = approvedModels.get(draft.model_revision_id);
    const template = approvedTemplates.get(draft.extraction_template_id);
    if (model === undefined || model.content_sha256 !== draft.model_revision_sha256) {
      fail("$.SnapshotManifest.normalized_records[].draft.model_revision_id", "model revision is not release-approved");
    }
    if (template === undefined || template.content_sha256 !== draft.extraction_template_sha256) {
      fail("$.SnapshotManifest.normalized_records[].draft.extraction_template_id", "template is not release-approved");
    }
    assertSame(template.model_revision_id, draft.model_revision_id, "$.SnapshotManifest.normalized_records[].draft.model_revision_id");
    assertSame(template.semantic_type_id, draft.semantic_type_id, "$.SnapshotManifest.normalized_records[].draft.semantic_type_id");
    assertSame(
      template.semantic_type_version,
      draft.semantic_type_version,
      "$.SnapshotManifest.normalized_records[].draft.semantic_type_version",
    );
    assertSame(template.payload_model_key, draft.payload_model_key, "$.SnapshotManifest.normalized_records[].draft.payload_model_key");
    assertSame(
      template.output_schema_sha256,
      draft.payload_schema_sha256,
      "$.SnapshotManifest.normalized_records[].draft.payload_schema_sha256",
    );
    assertSame(
      template.extractor_implementation_sha256,
      draft.producer_implementation_sha256,
      "$.SnapshotManifest.normalized_records[].draft.producer_implementation_sha256",
    );
  }
}

function assertCrossContractBindings(contracts: Record<ContractName, JsonObject>): void {
  const scope = contracts.CaptureScope;
  const manifest = contracts.CaptureManifest;
  const snapshot = contracts.SnapshotManifest;
  const release = contracts.ReleaseManifest;
  const request = nestedObject(snapshot, "request", "$.SnapshotManifest.request");
  const registry = nestedObject(snapshot, "registry_snapshot", "$.SnapshotManifest.registry_snapshot");

  assertSame(manifest.capture_scope_id, scope.capture_scope_id, "$.CaptureManifest.capture_scope_id");
  assertSame(manifest.capture_scope_sha256, scope.content_sha256, "$.CaptureManifest.capture_scope_sha256");
  assertSame(release.capture_scope_id, scope.capture_scope_id, "$.ReleaseManifest.capture_scope_id");
  assertSame(release.capture_scope_sha256, scope.content_sha256, "$.ReleaseManifest.capture_scope_sha256");
  assertSame(release.universe, scope.universe, "$.ReleaseManifest.universe");
  assertSame(request.universe, scope.universe, "$.SnapshotManifest.request.universe");
  assertSame(release.research_catalog_id, scope.research_catalog_id, "$.ReleaseManifest.research_catalog_id");
  assertSame(release.registry_snapshot_id, registry.registry_snapshot_id, "$.ReleaseManifest.registry_snapshot_id");
  assertSame(release.registry_snapshot_sha256, registry.content_sha256, "$.ReleaseManifest.registry_snapshot_sha256");
  assertSame(scope.source_registry_id, registry.source_registry_snapshot_id, "$.CaptureScope.source_registry_id");
  assertSame(
    scope.semantic_type_registry_id,
    registry.semantic_type_registry_snapshot_id,
    "$.CaptureScope.semantic_type_registry_id",
  );
  assertSame(
    release.identifier_type_registry_id,
    registry.identifier_type_registry_snapshot_id,
    "$.ReleaseManifest.identifier_type_registry_id",
  );

  const captureRequirements = new Map(
    objectArray(scope, "requirements", "$.CaptureScope.requirements").map((requirement) => [
      requirement.capture_requirement_id,
      requirement,
    ]),
  );
  for (const demand of objectArray(request, "demand_cells", "$.SnapshotManifest.request.demand_cells")) {
    const captureRequirement = captureRequirements.get(demand.capture_requirement_id);
    if (captureRequirement === undefined) {
      fail("$.SnapshotManifest.request.demand_cells[].capture_requirement_id", "unknown CaptureRequirement");
    }
    assertSame(
      demand.semantic_type_id,
      captureRequirement.semantic_type_id,
      "$.SnapshotManifest.request.demand_cells[].semantic_type_id",
    );
    assertSame(
      demand.semantic_type_version,
      captureRequirement.semantic_type_version,
      "$.SnapshotManifest.request.demand_cells[].semantic_type_version",
    );
    assertSame(demand.domain, captureRequirement.domain, "$.SnapshotManifest.request.demand_cells[].domain");
  }
  assertSnapshotExtractionBindings(snapshot, release);
}

function parseContractMap(value: unknown, path: string): Record<ContractName, JsonObject> {
  const object = asObject(value, path);
  assertExactKeys(object, CONTRACT_NAMES, path);
  return Object.fromEntries(
    CONTRACT_NAMES.map((name) => [name, asObject(object[name], `${path}.${name}`) as JsonObject]),
  ) as Record<ContractName, JsonObject>;
}

function parseHashMap(value: unknown, path: string): Record<ContractName, string> {
  const object = asObject(value, path);
  assertExactKeys(object, CONTRACT_NAMES, path);
  return Object.fromEntries(CONTRACT_NAMES.map((name) => [name, asString(object[name], `${path}.${name}`)])) as Record<
    ContractName,
    string
  >;
}

export async function validateIssue58Conformance(
  schemaInput: unknown,
  fixtureInput: unknown,
): Promise<Record<ContractName, JsonObject>> {
  const schemaBundle = asObject(schemaInput, "$.schema_bundle");
  const fixtureBundle = asObject(fixtureInput, "$.fixture_bundle");
  assertExactKeys(schemaBundle, ["contract_set", "schema_sha256", "schemas"], "$.schema_bundle");
  assertExactKeys(fixtureBundle, ["contract_set", "schema_sha256", "contracts"], "$.fixture_bundle");
  if (schemaBundle.contract_set !== CONTRACT_SET || fixtureBundle.contract_set !== CONTRACT_SET) {
    fail("$.contract_set", "unknown conformance contract set");
  }

  const schemas = parseContractMap(schemaBundle.schemas, "$.schema_bundle.schemas");
  const schemaHashes = parseHashMap(schemaBundle.schema_sha256, "$.schema_bundle.schema_sha256");
  const fixtureHashes = parseHashMap(fixtureBundle.schema_sha256, "$.fixture_bundle.schema_sha256");
  const contracts = parseContractMap(fixtureBundle.contracts, "$.fixture_bundle.contracts");

  for (const name of CONTRACT_NAMES) {
    const calculatedSchemaHash = await canonicalSha256(schemas[name]);
    if (schemaHashes[name] !== calculatedSchemaHash || fixtureHashes[name] !== calculatedSchemaHash) {
      fail(`$.schema_sha256.${name}`, "schema digest drift");
    }
    validateJsonSchema(schemas[name], contracts[name], `$.contracts.${name}`);
  }

  await validateCaptureIdentities(contracts.CaptureScope, contracts.CaptureManifest);
  await validateSnapshotIdentity(contracts.SnapshotManifest);
  await validateReleaseIdentity(contracts.ReleaseManifest);
  assertCrossContractBindings(contracts);
  return contracts;
}
