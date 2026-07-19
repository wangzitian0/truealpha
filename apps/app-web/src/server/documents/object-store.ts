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

export interface DocumentObjectRef {
  bucket: string;
  key: string;
  sha256: string;
  byteLength: number;
  contentType: string;
}

export class DocumentStorageError extends Error {}

function env(name: string, fallback: string): string {
  return process.env[name] ?? fallback;
}

let client: S3Client | null = null;

function getClient(): S3Client {
  if (!client) {
    client = new S3Client({
      endpoint: env("S3_ENDPOINT", "http://localhost:9000"),
      region: env("S3_REGION", "us-east-1"),
      forcePathStyle: true,
      credentials: {
        accessKeyId: env("S3_ACCESS_KEY", "minio"),
        secretAccessKey: env("S3_SECRET_KEY", "minio_local_secret"),
      },
    });
  }
  return client;
}

function bucket(): string {
  return env("S3_BUCKET", "truealpha-raw");
}

function documentsPrefix(): string {
  return env("S3_DOCUMENTS_PREFIX", "documents");
}

async function bodyToBuffer(body: unknown): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of body as AsyncIterable<Buffer | Uint8Array>) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

/** Content-addressed store: identical bytes always resolve to the same key,
 * so a re-render that produces byte-identical output is a no-op write —
 * `head_object` before `put_object`, exactly like the Python adapter. */
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
    if (head.ContentLength !== bytes.length) {
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
  const s3 = getClient();
  let bytes: Buffer;
  try {
    const response = await s3.send(new GetObjectCommand({ Bucket: ref.bucket, Key: ref.key }));
    bytes = await bodyToBuffer(response.Body);
  } catch (error) {
    throw new DocumentStorageError(`cannot read ${ref.key}: ${String(error)}`);
  }
  if (createHash("sha256").update(bytes).digest("hex") !== ref.sha256) {
    throw new DocumentStorageError(`checksum mismatch for ${ref.key}`);
  }
  return bytes;
}
