/**
 * #373: private document artifact storage over the same S3-compatible
 * object storage (MinIO locally, per docker-compose.yml) that
 * `libs/runtime/src/truealpha_runtime/storage.py`'s `S3RawObjectStore`
 * already uses for raw source capture — this is the first TypeScript-side
 * caller, mirroring that adapter's shape field-for-field (content-addressed
 * key, head-before-put dedup, checksum-verified read).
 *
 * This is NOT the raw-capture immutable pipeline CLAUDE.md's "apps never
 * use object storage as a service-to-service data path" red line guards —
 * that line protects `raw.fetches`/the source-capture lineage from being
 * bypassed by a typed consumer reaching into raw bytes directly. Documents
 * here are content the app itself renders and owns; this store is reached
 * only from server-side repository code (never a browser or MCP response —
 * see documents.ts), using a distinct bucket prefix (`S3_DOCUMENTS_PREFIX`)
 * so it can never collide with or be mistaken for the raw-capture prefix.
 */

import { createHash } from "node:crypto";
import {
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
  S3ServiceException,
} from "@aws-sdk/client-s3";
import { NodeHttpHandler } from "@smithy/node-http-handler";

export interface DocumentObjectRef {
  bucket: string;
  key: string;
  sha256: string;
  byteLength: number;
  contentType: string;
}

export class DocumentStorageError extends Error {}

function env(name: string, fallback: string): string {
  const value = process.env[name];
  return value === undefined || value === "" ? fallback : value;
}

/** A non-numeric S3_CONNECT_TIMEOUT_SECONDS must fall back to the default,
 * not silently produce a NaN timeout (which NodeHttpHandler would treat as
 * "no timeout" — the opposite of what this setting exists to guarantee). */
export function envPositiveSeconds(name: string, fallbackSeconds: number): number {
  const raw = Number(env(name, String(fallbackSeconds)));
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackSeconds;
}

let client: S3Client | null = null;
let testClientOverride: Pick<S3Client, "send"> | null = null;

/** Test-only injection point (documents-object-store.test.ts): lets a unit
 * test exercise the dedup/collision/checksum logic against a fake client
 * without a live MinIO, which this repo has no way to run in CI/sandboxed
 * environments today. Never called from production code. */
export function __setTestClient(overrideClient: Pick<S3Client, "send"> | null): void {
  testClientOverride = overrideClient;
}

function getClient(): S3Client {
  if (testClientOverride) return testClientOverride as S3Client;
  if (!client) {
    // Mirrors S3_CONNECT_TIMEOUT_SECONDS's role in the Python
    // S3RawObjectStore adapter — without an explicit timeout here, a
    // stalled S3/MinIO call could hang a server request indefinitely.
    const timeoutMs = envPositiveSeconds("S3_CONNECT_TIMEOUT_SECONDS", 5) * 1000;
    client = new S3Client({
      endpoint: env("S3_ENDPOINT", "http://localhost:9000"),
      region: env("S3_REGION", "us-east-1"),
      forcePathStyle: true,
      credentials: {
        accessKeyId: env("S3_ACCESS_KEY", "minio"),
        secretAccessKey: env("S3_SECRET_KEY", "minio_local_secret"),
      },
      requestHandler: new NodeHttpHandler({
        connectionTimeout: timeoutMs,
        socketTimeout: timeoutMs,
      }),
    });
  }
  return client;
}

export function bucket(): string {
  return env("S3_BUCKET", "truealpha-raw");
}

function documentsPrefix(): string {
  const prefix = env("S3_DOCUMENTS_PREFIX", "documents");
  const rawPrefix = env("S3_RAW_PREFIX", "raw");
  // The prefix guard in getDocumentArtifact only isolates the raw-capture
  // namespace if the two prefixes are actually distinct — a misconfigured
  // deployment setting them equal would silently defeat that isolation.
  if (prefix === rawPrefix) {
    throw new DocumentStorageError(
      `S3_DOCUMENTS_PREFIX must not equal S3_RAW_PREFIX (both are "${prefix}")`,
    );
  }
  return prefix;
}

async function bodyToBuffer(body: unknown): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of body as AsyncIterable<Buffer | Uint8Array>) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

/** Content-addressed store, scoped per owner (the key includes
 * `ownerPrincipalId`, so this is per-owner dedup, not global — two owners
 * uploading byte-identical content each get their own key). A re-render
 * that produces byte-identical output for the *same* owner is a no-op
 * write — `head_object` before `put_object`, exactly like the Python
 * adapter. */
export async function storeDocumentArtifact(
  ownerPrincipalId: string,
  bytes: Buffer,
  contentType: string,
): Promise<DocumentObjectRef> {
  const digest = createHash("sha256").update(bytes).digest("hex");
  const key = `${documentsPrefix()}/${ownerPrincipalId}/${digest.slice(0, 2)}/${digest}`;
  const s3 = getClient();

  let exists = false;
  try {
    const head = await s3.send(new HeadObjectCommand({ Bucket: bucket(), Key: key }));
    exists = true;
    // Length alone doesn't rule out a same-length collision at this key —
    // also compare the digest we stamped into Metadata on the original
    // write, so a mismatch (corruption/tampering at rest) is caught here
    // rather than silently treated as "already stored".
    if (head.ContentLength !== bytes.length || head.Metadata?.sha256 !== digest) {
      throw new DocumentStorageError(`content-address collision for ${key}`);
    }
  } catch (error) {
    if (error instanceof DocumentStorageError) throw error;
    const notFound = error instanceof S3ServiceException && error.$metadata.httpStatusCode === 404;
    if (!notFound) {
      throw new DocumentStorageError(`cannot inspect ${key}: ${String(error)}`);
    }
  }

  if (!exists) {
    try {
      await s3.send(
        new PutObjectCommand({
          Bucket: bucket(),
          Key: key,
          Body: bytes,
          ContentType: contentType,
          Metadata: { sha256: digest },
        }),
      );
    } catch (error) {
      throw new DocumentStorageError(`cannot store ${key}: ${String(error)}`);
    }
  }

  return { bucket: bucket(), key, sha256: digest, byteLength: bytes.length, contentType };
}

/** Reads back a stored artifact, re-verifying the checksum — a mismatch
 * means the bytes were corrupted or tampered with at rest, never silently
 * served. */
export async function getDocumentArtifact(ref: DocumentObjectRef): Promise<Buffer> {
  if (ref.bucket !== bucket()) {
    throw new DocumentStorageError(`object belongs to unexpected bucket ${ref.bucket}`);
  }
  // Same bucket also holds the raw-capture pipeline's objects under a
  // different prefix (S3_RAW_PREFIX) — this exported function must never
  // become a path to reading those, even given an otherwise well-formed ref.
  if (!ref.key.startsWith(`${documentsPrefix()}/`)) {
    throw new DocumentStorageError(`object key ${ref.key} is outside the documents prefix`);
  }
  const s3 = getClient();
  let responseBody: unknown;
  try {
    const response = await s3.send(new GetObjectCommand({ Bucket: ref.bucket, Key: ref.key }));
    responseBody = response.Body;
  } catch (error) {
    throw new DocumentStorageError(`cannot read ${ref.key}: ${String(error)}`);
  }
  if (!responseBody) {
    throw new DocumentStorageError(`empty response body for ${ref.key}`);
  }
  const bytes = await bodyToBuffer(responseBody);
  if (bytes.length !== ref.byteLength) {
    throw new DocumentStorageError(`byte length mismatch for ${ref.key}: expected ${ref.byteLength}, got ${bytes.length}`);
  }
  if (createHash("sha256").update(bytes).digest("hex") !== ref.sha256) {
    throw new DocumentStorageError(`checksum mismatch for ${ref.key}`);
  }
  return bytes;
}
