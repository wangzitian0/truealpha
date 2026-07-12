import { readFileSync } from "node:fs";

import {
  ContractValidationError,
  canonicalSha256,
  type JsonObject,
  validateIssue58Conformance,
} from "../src/contracts/issue58";

const schemaUrl = new URL("../../../libs/contracts/conformance/issue58.schemas.json", import.meta.url);
const fixtureUrl = new URL("../../../libs/contracts/conformance/issue58.fixtures.json", import.meta.url);

function readJson(url: URL): unknown {
  return JSON.parse(readFileSync(url, "utf8")) as unknown;
}

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function object(value: unknown, label: string): Record<string, unknown> {
  assert(typeof value === "object" && value !== null && !Array.isArray(value), `${label} must be an object`);
  return value as Record<string, unknown>;
}

function nested(value: unknown, ...keys: string[]): Record<string, unknown> {
  let current = object(value, "root");
  for (const key of keys) current = object(current[key], keys.join("."));
  return current;
}

function firstObject(value: Record<string, unknown>, key: string): Record<string, unknown> {
  const items = value[key];
  assert(Array.isArray(items) && items.length > 0, `${key} must contain an object`);
  return object(items[0], `${key}[0]`);
}

async function rehashIdentity(
  value: Record<string, unknown>,
  idField: string,
  hashField: string,
  prefix: string,
  excluded: readonly string[] = [],
): Promise<void> {
  const omitted = new Set([idField, hashField, ...excluded]);
  const payload = Object.fromEntries(Object.entries(value).filter(([key]) => !omitted.has(key))) as JsonObject;
  const digest = await canonicalSha256(payload);
  value[idField] = `${prefix}:${digest}`;
  value[hashField] = digest;
}

async function rehashCaptureManifest(bundle: unknown): Promise<Record<string, unknown>> {
  const manifest = nested(bundle, "contracts", "CaptureManifest");
  const cell = firstObject(manifest, "cells");
  const evidence = firstObject(cell, "evidence");
  await rehashIdentity(evidence, "evidence_id", "content_sha256", "capture-evidence");
  await rehashIdentity(cell, "capture_cell_id", "content_sha256", "capture-cell");
  await rehashIdentity(manifest, "capture_manifest_id", "content_sha256", "capture-manifest");
  return evidence;
}

async function rehashSnapshotManifest(bundle: unknown): Promise<Record<string, unknown>> {
  const snapshot = nested(bundle, "contracts", "SnapshotManifest");
  const record = firstObject(snapshot, "normalized_records");
  const originalRecordId = record.normalized_record_id;
  const draft = object(record.draft, "normalized_records[0].draft");
  await rehashIdentity(draft, "semantic_draft_id", "content_sha256", "semantic-draft");
  await rehashIdentity(record, "normalized_record_id", "content_sha256", "normalized-record");
  const selections = snapshot.selections;
  assert(Array.isArray(selections), "snapshot selections must be an array");
  for (const selectionValue of selections) {
    const selection = object(selectionValue, "selection");
    const recordIds = selection.normalized_record_ids;
    assert(Array.isArray(recordIds), "selection normalized_record_ids must be an array");
    selection.normalized_record_ids = recordIds.map((recordId) =>
      recordId === originalRecordId ? record.normalized_record_id : recordId,
    );
  }
  await rehashIdentity(snapshot, "snapshot_id", "content_sha256", "snapshot");
  return draft;
}

async function rehashReleaseManifest(bundle: unknown): Promise<Record<string, unknown>> {
  const release = nested(bundle, "contracts", "ReleaseManifest");
  const modelRevision = firstObject(release, "approved_model_revisions");
  await rehashIdentity(modelRevision, "model_revision_id", "content_sha256", "model-revision");
  const template = firstObject(release, "approved_extraction_templates");
  template.model_revision_id = modelRevision.model_revision_id;
  template.model_revision_sha256 = modelRevision.content_sha256;
  await rehashIdentity(template, "extraction_template_id", "content_sha256", "extraction-template");
  await rehashIdentity(
    release,
    "release_manifest_id",
    "manifest_sha256",
    "release-manifest",
    ["manifest_signature_ref"],
  );
  return template;
}

async function expectRejected(label: string, action: () => Promise<unknown>, message: RegExp): Promise<void> {
  try {
    await action();
  } catch (error) {
    assert(error instanceof ContractValidationError, `${label} raised the wrong error type`);
    assert(message.test(error.message), `${label} failed for the wrong reason: ${error.message}`);
    return;
  }
  throw new Error(`${label} was accepted`);
}

const schemaBundle = readJson(schemaUrl);
const fixtureBundle = readJson(fixtureUrl);
const contracts = await validateIssue58Conformance(schemaBundle, fixtureBundle);
assert(contracts.CaptureScope.capture_scope_id === contracts.CaptureManifest.capture_scope_id, "scope binding drift");

const unknownTopLevel = structuredClone(fixtureBundle);
nested(unknownTopLevel, "contracts", "CaptureScope").unexpected = "drift";
await expectRejected(
  "unknown top-level DTO field",
  () => validateIssue58Conformance(schemaBundle, unknownTopLevel),
  /unknown field unexpected/,
);

const unknownNested = structuredClone(fixtureBundle);
nested(unknownNested, "contracts", "CaptureScope", "universe").unexpected = "drift";
await expectRejected(
  "unknown nested DTO field",
  () => validateIssue58Conformance(schemaBundle, unknownNested),
  /unknown field unexpected/,
);

const staleReleaseShape = structuredClone(fixtureBundle);
nested(staleReleaseShape, "contracts", "ReleaseManifest").accepted_capture_manifest_ids = [];
await expectRejected(
  "stale post-run ReleaseManifest evidence",
  () => validateIssue58Conformance(schemaBundle, staleReleaseShape),
  /unknown field accepted_capture_manifest_ids/,
);

const staleTemplateIdShape = structuredClone(fixtureBundle);
nested(staleTemplateIdShape, "contracts", "ReleaseManifest").approved_extraction_template_ids = [];
await expectRejected(
  "stale template ID-only ReleaseManifest binding",
  () => validateIssue58Conformance(schemaBundle, staleTemplateIdShape),
  /unknown field approved_extraction_template_ids/,
);

const missingRequiredField = structuredClone(fixtureBundle);
const missingFieldEvidence = nested(missingRequiredField, "contracts", "CaptureManifest");
delete firstObject(firstObject(missingFieldEvidence, "cells"), "evidence").populated_fields;
await rehashCaptureManifest(missingRequiredField);
await expectRejected(
  "missing required populated-fields evidence",
  () => validateIssue58Conformance(schemaBundle, missingRequiredField),
  /missing required populated-fields evidence/,
);

const wrongSemanticType = structuredClone(fixtureBundle);
const wrongTypeEvidence = await rehashCaptureManifest(wrongSemanticType);
wrongTypeEvidence.semantic_type_id = "semantic.other-type";
await rehashCaptureManifest(wrongSemanticType);
await expectRejected(
  "wrong capture semantic type",
  () => validateIssue58Conformance(schemaBundle, wrongSemanticType),
  /semantic type mismatch/,
);

const failedQuality = structuredClone(fixtureBundle);
const failedQualityEvidence = await rehashCaptureManifest(failedQuality);
failedQualityEvidence.quality_status = "fail";
await rehashCaptureManifest(failedQuality);
await expectRejected(
  "failed capture quality",
  () => validateIssue58Conformance(schemaBundle, failedQuality),
  /quality status is not pass/,
);

const missingInvocationBinding = structuredClone(fixtureBundle);
const missingInvocationDraft = await rehashSnapshotManifest(missingInvocationBinding);
missingInvocationDraft.extraction_invocation_id = null;
missingInvocationDraft.extraction_invocation_sha256 = null;
await rehashSnapshotManifest(missingInvocationBinding);
await expectRejected(
  "incomplete versioned extraction invocation binding",
  () => validateIssue58Conformance(schemaBundle, missingInvocationBinding),
  /versioned extraction binding is incomplete/,
);

const unapprovedTemplateBinding = structuredClone(fixtureBundle);
const unapprovedTemplateDraft = await rehashSnapshotManifest(unapprovedTemplateBinding);
const unapprovedTemplateDigest = "f".repeat(64);
unapprovedTemplateDraft.extraction_template_id = `extraction-template:${unapprovedTemplateDigest}`;
unapprovedTemplateDraft.extraction_template_sha256 = unapprovedTemplateDigest;
await rehashSnapshotManifest(unapprovedTemplateBinding);
await expectRejected(
  "unapproved extraction template replay",
  () => validateIssue58Conformance(schemaBundle, unapprovedTemplateBinding),
  /template is not release-approved/,
);

const deterministicWithExtraction = structuredClone(fixtureBundle);
const deterministicDraft = await rehashSnapshotManifest(deterministicWithExtraction);
deterministicDraft.producer_kind = "deterministic_normalizer";
await rehashSnapshotManifest(deterministicWithExtraction);
await expectRejected(
  "deterministic normalizer with extraction bindings",
  () => validateIssue58Conformance(schemaBundle, deterministicWithExtraction),
  /deterministic normalizer carries extraction bindings/,
);

const mutableTemplateVersion = structuredClone(fixtureBundle);
const mutableTemplate = await rehashReleaseManifest(mutableTemplateVersion);
mutableTemplate.template_version = "v2-latest";
await rehashReleaseManifest(mutableTemplateVersion);
await expectRejected(
  "mutable extraction template version",
  () => validateIssue58Conformance(schemaBundle, mutableTemplateVersion),
  /mutable extraction template version/,
);

const mutableModelRevision = structuredClone(fixtureBundle);
const mutableModel = firstObject(
  nested(mutableModelRevision, "contracts", "ReleaseManifest"),
  "approved_model_revisions",
);
mutableModel.immutable_revision = "2026-current";
await rehashReleaseManifest(mutableModelRevision);
await expectRejected(
  "mutable model revision",
  () => validateIssue58Conformance(schemaBundle, mutableModelRevision),
  /mutable model revision/,
);

for (const [contractName, mutatedField] of [
  ["CaptureScope", "owner"],
  ["CaptureManifest", "partition_key"],
  ["SnapshotManifest", "resolver_version"],
  ["ReleaseManifest", "contract_version"],
] as const) {
  const tampered = structuredClone(fixtureBundle);
  nested(tampered, "contracts", contractName)[mutatedField] = "tampered";
  await expectRejected(
    `${contractName} content tampering`,
    () => validateIssue58Conformance(schemaBundle, tampered),
    /content hash mismatch/,
  );
}

const driftedSchema = structuredClone(schemaBundle);
nested(driftedSchema, "schemas", "CaptureScope").title = "DriftedCaptureScope";
await expectRejected(
  "schema drift",
  () => validateIssue58Conformance(driftedSchema, fixtureBundle),
  /schema digest drift/,
);

const substitutedRelease = structuredClone(fixtureBundle);
const release = nested(substitutedRelease, "contracts", "ReleaseManifest");
const substituteDigest = "0".repeat(64);
release.capture_scope_id = `capture-scope:${substituteDigest}`;
release.capture_scope_sha256 = substituteDigest;
const releasePayload = Object.fromEntries(
  Object.entries(release).filter(
    ([key]) => !["release_manifest_id", "manifest_sha256", "manifest_signature_ref"].includes(key),
  ),
) as JsonObject;
const substitutedReleaseHash = await canonicalSha256(releasePayload);
release.release_manifest_id = `release-manifest:${substitutedReleaseHash}`;
release.manifest_sha256 = substitutedReleaseHash;
await expectRejected(
  "individually valid substituted release binding",
  () => validateIssue58Conformance(schemaBundle, substitutedRelease),
  /cross-contract binding mismatch/,
);

console.log("Issue #58 Python/TypeScript conformance passed");
