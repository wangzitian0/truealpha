/** Private content-addressed document artifact storage over S3/MinIO (#373). */

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

export function envPositiveSeconds(name: string, fallbackSeconds: number): number {
  const raw = Number(env(name, String(fallbackSeconds)));
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackSeconds;
}

let client: S3Client | null = null;
let testClientOverride: Pick<S3Client, "send"> | null = null;

export function __setTestClient(overrideClient: Pick<S3Client, "send"> | null): void {
  testClientOverride = overrideClient;
}

function requireEnv(name: string): string {
  const value = process.env[name]?.trim() ?? "";
  if (value === "") {
    throw new DocumentStorageError(
      `${name} is not set: document object storage requires an explicit S3 configuration (#1367)`,
    );
  }
  return value;
}

function getClient(): S3Client {
  if (testClientOverride) return testClientOverride as S3Client;
  if (!client) {
    const timeoutMs = envPositiveSeconds("S3_CONNECT_TIMEOUT_SECONDS", 5) * 1000;
    client = new S3Client({
      endpoint: requireEnv("S3_ENDPOINT"),
      region: env("S3_REGION", "us-east-1"),
      forcePathStyle: true,
      credentials: { accessKeyId: requireEnv("S3_ACCESS_KEY"), secretAccessKey: requireEnv("S3_SECRET_KEY") },
      requestHandler: new NodeHttpHandler({ connectionTimeout: timeoutMs, socketTimeout: timeoutMs }),
    });
  }
  return client;
}

export function bucket(): string {
  return env("S3_BUCKET", "truealpha-raw");
}

function normalizedPrefix(raw: string): string {
  return raw.replace(/^\/+|\/+$/g, "");
}

function documentsPrefix(): string {
  const prefix = normalizedPrefix(env("S3_DOCUMENTS_PREFIX", "documents"));
  const rawPrefix = normalizedPrefix(env("S3_RAW_PREFIX", "raw"));
  if (prefix.length === 0 || `${prefix}/`.startsWith(`${rawPrefix}/`) || `${rawPrefix}/`.startsWith(`${prefix}/`)) {
    throw new DocumentStorageError(
      `S3_DOCUMENTS_PREFIX ("${prefix}") must be a distinct, non-empty path segment from S3_RAW_PREFIX ("${rawPrefix}")`,
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
    if (head.ContentLength !== bytes.length || head.Metadata?.sha256 !== digest) {
      throw new DocumentStorageError(`content-address collision for ${key}`);
    }
  } catch (error) {
    if (error instanceof DocumentStorageError) throw error;
    const notFound = error instanceof S3ServiceException && error.$metadata.httpStatusCode === 404;
    if (!notFound) throw new DocumentStorageError(`cannot inspect ${key}: ${String(error)}`);
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

export async function getDocumentArtifact(ref: DocumentObjectRef): Promise<Buffer> {
  if (ref.bucket !== bucket()) {
    throw new DocumentStorageError(`object belongs to unexpected bucket ${ref.bucket}`);
  }
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
  if (!responseBody) throw new DocumentStorageError(`empty response body for ${ref.key}`);
  const bytes = await bodyToBuffer(responseBody);
  if (bytes.length !== ref.byteLength) {
    throw new DocumentStorageError(`byte length mismatch for ${ref.key}: expected ${ref.byteLength}, got ${bytes.length}`);
  }
  if (createHash("sha256").update(bytes).digest("hex") !== ref.sha256) {
    throw new DocumentStorageError(`checksum mismatch for ${ref.key}`);
  }
  return bytes;
}
