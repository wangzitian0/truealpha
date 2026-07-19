/**
 * #373: exercises storeDocumentArtifact/getDocumentArtifact against a fake
 * S3Client (via __setTestClient), since this repo has no way to run a live
 * MinIO in CI/sandboxed environments. Covers the paths a real MinIO would
 * otherwise be the only way to reach: head-before-put dedup, the
 * content-length/metadata collision check, and checksum-verified reads.
 *
 * Run standalone: `bun run tests/documents-object-store.test.ts`.
 */

import { createHash } from "node:crypto";
import { GetObjectCommand, HeadObjectCommand, PutObjectCommand, S3ServiceException } from "@aws-sdk/client-s3";
import {
  __setTestClient,
  DocumentStorageError,
  envPositiveSeconds,
  getDocumentArtifact,
  storeDocumentArtifact,
} from "../src/server/documents/object-store";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function assertThrows(fn: () => Promise<unknown>, message: string): Promise<void> {
  try {
    await fn();
  } catch (error) {
    assert(error instanceof DocumentStorageError, `${message} (expected a DocumentStorageError, got ${String(error)})`);
    return;
  }
  throw new Error(message);
}

interface StoredObject {
  body: Buffer;
  contentType: string;
  sha256: string | undefined;
}

class FakeS3Client {
  objects = new Map<string, StoredObject>();
  putCalls = 0;

  private notFound(): never {
    throw new S3ServiceException({
      name: "NotFound",
      $fault: "client",
      $metadata: { httpStatusCode: 404 },
    });
  }

  async send(command: unknown): Promise<unknown> {
    if (command instanceof HeadObjectCommand) {
      const key = command.input.Key ?? "";
      const object = this.objects.get(key);
      if (!object) this.notFound();
      return { ContentLength: object.body.length, Metadata: { sha256: object.sha256 } };
    }
    if (command instanceof PutObjectCommand) {
      this.putCalls += 1;
      const key = command.input.Key ?? "";
      const body = command.input.Body as Buffer;
      this.objects.set(key, {
        body: Buffer.isBuffer(body) ? body : Buffer.from(body as Uint8Array),
        contentType: String(command.input.ContentType),
        sha256: command.input.Metadata?.sha256,
      });
      return {};
    }
    if (command instanceof GetObjectCommand) {
      const key = command.input.Key ?? "";
      const object = this.objects.get(key);
      if (!object) this.notFound();
      return {
        Body: (async function* () {
          yield object.body;
        })(),
      };
    }
    throw new Error(`FakeS3Client: unexpected command ${String(command)}`);
  }
}

// Mirrors object-store.ts's internal key formula so a test can seed a
// collision at the exact key storeDocumentArtifact will compute.
function contentKey(ownerPrincipalId: string, bytes: Buffer): string {
  const digest = createHash("sha256").update(bytes).digest("hex");
  return `documents/${ownerPrincipalId}/${digest.slice(0, 2)}/${digest}`;
}

async function run() {
  const fake = new FakeS3Client();
  __setTestClient(fake);

  const bytes = Buffer.from('{"gppe": 1.5}');
  const ref = await storeDocumentArtifact("principal:alice", bytes, "application/json");
  assert(fake.putCalls === 1, "the first store must PUT once");
  assert(ref.sha256 === createHash("sha256").update(bytes).digest("hex"), "the ref must carry the real digest");
  assert(ref.byteLength === bytes.length, "the ref must carry the real byte length");

  const refAgain = await storeDocumentArtifact("principal:alice", bytes, "application/json");
  assert(fake.putCalls === 1, "re-storing identical bytes for the same owner must dedup, not PUT again");
  assert(refAgain.key === ref.key, "a dedup write must resolve to the same content-addressed key");

  const readBack = await getDocumentArtifact(ref);
  assert(readBack.equals(bytes), "a checksum-verified read must return the exact bytes that were stored");

  await assertThrows(
    () => getDocumentArtifact({ ...ref, sha256: "f".repeat(64) }),
    "a ref with the wrong sha256 must be rejected as a checksum mismatch",
  );

  await assertThrows(
    () => getDocumentArtifact({ ...ref, byteLength: ref.byteLength + 1 }),
    "a ref with the wrong byteLength must be rejected before the checksum check even runs",
  );

  await assertThrows(
    () => getDocumentArtifact({ ...ref, bucket: "some-other-bucket" }),
    "a ref naming an unexpected bucket must be rejected without calling S3 at all",
  );

  await assertThrows(
    () => getDocumentArtifact({ ...ref, key: "raw/some-source/aa/aaaa" }),
    "a ref pointing outside the documents prefix (e.g. the raw-capture prefix) must be rejected",
  );

  // Seed a same-length, different-content object directly at the key a
  // fresh write would compute, with metadata sha256 left stale — simulates
  // corruption/tampering at rest that a content-length-only check would miss.
  const collideOwner = "principal:bob";
  const collideBytes = Buffer.from("aaaaaaaaaa");
  const key = contentKey(collideOwner, collideBytes);
  fake.objects.set(key, {
    body: Buffer.from("bbbbbbbbbb"),
    contentType: "application/json",
    sha256: createHash("sha256").update(Buffer.from("bbbbbbbbbb")).digest("hex"),
  });
  await assertThrows(
    () => storeDocumentArtifact(collideOwner, collideBytes, "application/json"),
    "a same-length-different-metadata object already at the content-addressed key must be treated as a collision",
  );

  const originalDocumentsPrefix = process.env.S3_DOCUMENTS_PREFIX;
  try {
    // S3_RAW_PREFIX defaults to "raw" (see .env.example); each of these
    // must be rejected, not just an exact string match, since the
    // isolation getDocumentArtifact's prefix guard provides depends on the
    // two prefixes never sharing a path segment in either direction.
    delete process.env.S3_RAW_PREFIX;
    for (const bad of ["raw", "raw/", "/raw", "raw/documents"]) {
      process.env.S3_DOCUMENTS_PREFIX = bad;
      await assertThrows(
        () => storeDocumentArtifact("principal:carol", Buffer.from("x"), "text/plain"),
        `S3_DOCUMENTS_PREFIX=${JSON.stringify(bad)} colliding with S3_RAW_PREFIX must be rejected, not silently accepted`,
      );
    }
    // "" alone falls back to the default via env()'s blank-is-unset rule,
    // so use a prefix that normalizes to empty (all slashes) to actually
    // exercise the empty-after-normalization branch.
    process.env.S3_DOCUMENTS_PREFIX = "///";
    await assertThrows(
      () => storeDocumentArtifact("principal:carol", Buffer.from("x"), "text/plain"),
      "an S3_DOCUMENTS_PREFIX that normalizes to empty must be rejected",
    );
  } finally {
    if (originalDocumentsPrefix === undefined) delete process.env.S3_DOCUMENTS_PREFIX;
    else process.env.S3_DOCUMENTS_PREFIX = originalDocumentsPrefix;
  }

  __setTestClient(null);

  const originalTimeout = process.env.S3_CONNECT_TIMEOUT_SECONDS;
  try {
    delete process.env.S3_CONNECT_TIMEOUT_SECONDS;
    assert(envPositiveSeconds("S3_CONNECT_TIMEOUT_SECONDS", 5) === 5, "an unset timeout must fall back to the default");

    process.env.S3_CONNECT_TIMEOUT_SECONDS = "10";
    assert(envPositiveSeconds("S3_CONNECT_TIMEOUT_SECONDS", 5) === 10, "a valid positive timeout must be used as-is");

    for (const bad of ["not-a-number", "0", "-3", "NaN", "Infinity"]) {
      process.env.S3_CONNECT_TIMEOUT_SECONDS = bad;
      assert(
        envPositiveSeconds("S3_CONNECT_TIMEOUT_SECONDS", 5) === 5,
        `a non-finite/non-positive timeout ${JSON.stringify(bad)} must fall back to the default, not become NaN`,
      );
    }
  } finally {
    if (originalTimeout === undefined) delete process.env.S3_CONNECT_TIMEOUT_SECONDS;
    else process.env.S3_CONNECT_TIMEOUT_SECONDS = originalTimeout;
  }

  console.log("documents-object-store.test.ts: all assertions passed");
}

run();
