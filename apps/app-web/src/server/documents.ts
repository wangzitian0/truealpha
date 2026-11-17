/**
 * Owner-scoped research document lifecycle over `app.research_documents` and related tables.
 */

import { randomUUID } from "node:crypto";
import type { AccessContext } from "@/contracts/strategyRun";
import { withOwnerScopedRuntime } from "@/server/auth/db";
import { bucket, getDocumentArtifact, storeDocumentArtifact } from "@/server/documents/object-store";

export type ResearchDocument = { documentId: string; tenantId: string; ownerPrincipalId: string; createdAt: string };
export type ResearchDocumentRevision = {
  revisionId: string; documentId: string; tenantId: string; ownerPrincipalId: string;
  sourceArtifactId: string; artifactSha256: string; artifactByteLength: number; artifactContentType: string; createdAt: string;
};
export type DocumentTombstone = { tombstoneId: string; documentId: string; createdAt: string };
export type DocumentDownloadTicket = { ticketId: string; documentId: string; revisionId: string; expiresAt: string };
export type NewDocumentRevisionInput = { sourceArtifactId: string; bytes: Buffer; contentType: string };
export type DocumentCursor = { createdAt: string; documentId: string };
export type DocumentListQuery = { limit?: number; before?: DocumentCursor | null };
export type DocumentPage = { documents: ResearchDocument[]; nextBefore: DocumentCursor | null };

const DEFAULT_LIST_LIMIT = 50;
const MAX_LIST_LIMIT = 200;
const ISO_DATE_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const MUTABLE_TOKENS = new Set(["latest", "current", "default", "head"]);
const POSTGRES_FOREIGN_KEY_VIOLATION = "23503";

export const STABLE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;

export function clampListLimit(limit: number | undefined): number {
  if (limit === undefined) return DEFAULT_LIST_LIMIT;
  if (!Number.isFinite(limit) || !Number.isInteger(limit)) throw new Error("limit must be a finite integer");
  return Math.min(Math.max(1, limit), MAX_LIST_LIMIT);
}

const idOf = (prefix: string) => `${prefix}:${randomUUID()}`;

export function assertPositiveInteger(value: number, fieldName: string): number {
  if (!Number.isInteger(value) || value <= 0) throw new Error(`${fieldName} must be a positive integer`);
  return value;
}

export function assertNonEmptyText(value: string, fieldName: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) throw new Error(`${fieldName} must not be empty`);
  return trimmed;
}

export function assertStableId(value: string, fieldName: string): string {
  if (!STABLE_ID_PATTERN.test(value)) throw new Error(`${fieldName} must be a stable identifier`);
  const tokens = value.toLowerCase().split(/[._:/@+-]/).filter(Boolean);
  if (tokens.some((token) => MUTABLE_TOKENS.has(token))) throw new Error(`${fieldName} must name an immutable version`);
  return value;
}

function validateNewRevision(revision: NewDocumentRevisionInput): NewDocumentRevisionInput {
  return {
    ...revision,
    sourceArtifactId: assertStableId(revision.sourceArtifactId, "sourceArtifactId"),
    contentType: assertNonEmptyText(revision.contentType, "contentType"),
  };
}

export function parseBeforeCursor(
  before: DocumentCursor | null | undefined,
): { createdAt: string; documentId: string } | null {
  if (before === undefined || before === null) return null;
  if (!ISO_DATE_TIME_PATTERN.test(before.createdAt) || Number.isNaN(new Date(before.createdAt).getTime())) {
    throw new Error("before.createdAt must be a valid ISO datetime");
  }
  return { createdAt: before.createdAt, documentId: assertStableId(before.documentId, "before.documentId") };
}

export interface DocumentsRepository {
  listDocuments(context: AccessContext, query: DocumentListQuery): Promise<DocumentPage>;
  getDocument(context: AccessContext, documentId: string): Promise<ResearchDocument | null>;
  listRevisions(context: AccessContext, documentId: string): Promise<ResearchDocumentRevision[]>;
  createDocument(context: AccessContext, revision: NewDocumentRevisionInput): Promise<{ document: ResearchDocument; revision: ResearchDocumentRevision }>;
  appendRevision(context: AccessContext, documentId: string, revision: NewDocumentRevisionInput): Promise<ResearchDocumentRevision>;
  tombstoneDocument(context: AccessContext, documentId: string): Promise<DocumentTombstone>;
  issueDownloadTicket(context: AccessContext, documentId: string, revisionId: string, expiresInMinutes: number): Promise<DocumentDownloadTicket>;
  redeemDownloadTicket(context: AccessContext, ticketId: string): Promise<Buffer | null>;
  getRevisionBytes?(context: AccessContext, documentId: string, revisionId: string): Promise<Buffer | null>;
}

interface DocumentRow {
  document_id: string;
  tenant_id: string;
  owner_principal_id: string;
  created_at: Date;
}

interface RevisionObjectRow {
  object_key: string;
  artifact_sha256: string;
  artifact_byte_length: string;
  artifact_content_type: string;
}

interface RevisionRow extends RevisionObjectRow {
  revision_id: string;
  document_id: string;
  tenant_id: string;
  owner_principal_id: string;
  source_artifact_id: string;
  created_at: Date;
}

interface TombstoneRow {
  tombstone_id: string;
  document_id: string;
  created_at: Date;
}

type RuntimeClient = Parameters<Parameters<typeof withOwnerScopedRuntime>[1]>[0];

const DOC_COLS = "document_id, tenant_id, owner_principal_id, created_at";
const REV_OBJ_COLS = "object_key, artifact_sha256, artifact_byte_length, artifact_content_type";
const REV_COLS = `revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id, ${REV_OBJ_COLS}, created_at`;
const notTombstoned = (docIdExpr: string) =>
  `not exists (select 1 from app.research_document_tombstones ts where ts.document_id = ${docIdExpr})`;

const scoped = <T>(context: AccessContext, run: (client: RuntimeClient) => Promise<T>): Promise<T> =>
  withOwnerScopedRuntime({ tenantId: context.tenantId, principalId: context.principalId }, run);

async function throwNotFoundOnFk<T>(run: () => Promise<T>): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (isForeignKeyViolation(error)) throw new DocumentNotFoundError();
    throw error;
  }
}

async function fetchRevisionBytes(row: RevisionObjectRow | undefined): Promise<Buffer | null> {
  if (!row) return null;
  return getDocumentArtifact({
    bucket: bucket(),
    key: row.object_key,
    sha256: row.artifact_sha256,
    byteLength: parseByteLength(row.artifact_byte_length),
    contentType: row.artifact_content_type,
  });
}

async function readRevisionObject(client: RuntimeClient, whereClause: string, params: unknown[]): Promise<Buffer | null> {
  const result = await client.query<RevisionObjectRow>(
    `select ${REV_OBJ_COLS} from app.research_document_revisions where ${whereClause} and ${notTombstoned("document_id")}`,
    params,
  );
  return fetchRevisionBytes(result.rows[0]);
}

async function saveRevision(
  client: RuntimeClient,
  context: AccessContext,
  documentId: string,
  revision: NewDocumentRevisionInput,
): Promise<ResearchDocumentRevision> {
  const validated = validateNewRevision(revision);
  const objectRef = await storeDocumentArtifact(context.principalId, validated.bytes, validated.contentType);
  return insertRevision(client, context, documentId, validated, objectRef);
}

export class PostgresDocumentsRepository implements DocumentsRepository {
  async listDocuments(context: AccessContext, query: DocumentListQuery): Promise<DocumentPage> {
    const limit = clampListLimit(query.limit);
    const before = parseBeforeCursor(query.before);
    return scoped(context, async (client) => {
      const result = await client.query<DocumentRow & { created_at_cursor: string }>(
        `select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at,
                to_char(d.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') as created_at_cursor
         from app.research_documents d
         where ${notTombstoned("d.document_id")}
           and ($1::timestamptz is null or (d.created_at, d.document_id) < ($1::timestamptz, $2))
         order by d.created_at desc, d.document_id desc limit $3`,
        [before?.createdAt ?? null, before?.documentId ?? null, limit],
      );
      const documents = result.rows.map(rowToDocument);
      const lastRow = result.rows[result.rows.length - 1];
      return {
        documents,
        nextBefore: documents.length === limit && lastRow ? { createdAt: lastRow.created_at_cursor, documentId: lastRow.document_id } : null,
      };
    });
  }

  async getDocument(context: AccessContext, documentId: string): Promise<ResearchDocument | null> {
    return scoped(context, async (client) => {
      const result = await client.query<DocumentRow>(
        `select ${DOC_COLS} from app.research_documents where document_id = $1 and ${notTombstoned("document_id")}`,
        [documentId],
      );
      return result.rows[0] ? rowToDocument(result.rows[0]) : null;
    });
  }

  async listRevisions(context: AccessContext, documentId: string): Promise<ResearchDocumentRevision[]> {
    return scoped(context, async (client) => {
      const result = await client.query<RevisionRow>(
        `select ${REV_COLS} from app.research_document_revisions
         where document_id = $1 and ${notTombstoned("document_id")}
         order by created_at asc, revision_id asc`,
        [documentId],
      );
      return result.rows.map(rowToRevision);
    });
  }

  async createDocument(
    context: AccessContext,
    revision: NewDocumentRevisionInput,
  ): Promise<{ document: ResearchDocument; revision: ResearchDocumentRevision }> {
    return scoped(context, async (client) => {
      const documentId = idOf("document");
      const documentResult = await client.query<DocumentRow>(
        `insert into app.research_documents (document_id, tenant_id, owner_principal_id) values ($1, $2, $3) returning ${DOC_COLS}`,
        [documentId, context.tenantId, context.principalId],
      );
      const revisionRow = await saveRevision(client, context, documentId, revision);
      return { document: rowToDocument(documentResult.rows[0]), revision: revisionRow };
    });
  }

  async appendRevision(
    context: AccessContext,
    documentId: string,
    revision: NewDocumentRevisionInput,
  ): Promise<ResearchDocumentRevision> {
    return scoped(context, async (client) => {
      const exists = await client.query(
        `select 1 from app.research_documents where document_id = $1 and ${notTombstoned("document_id")}`,
        [documentId],
      );
      if (exists.rowCount === 0) throw new DocumentNotFoundError();
      return saveRevision(client, context, documentId, revision);
    });
  }

  async tombstoneDocument(context: AccessContext, documentId: string): Promise<DocumentTombstone> {
    return scoped(context, async (client) => {
      const inserted = await throwNotFoundOnFk(() =>
        client.query<TombstoneRow>(
          `insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id)
           values ($1, $2, $3, $4) on conflict (document_id) do nothing
           returning tombstone_id, document_id, created_at`,
          [idOf("tombstone"), documentId, context.tenantId, context.principalId],
        ),
      );
      if (inserted.rows[0]) return rowToTombstone(inserted.rows[0]);
      const existing = await client.query<TombstoneRow>(
        "select tombstone_id, document_id, created_at from app.research_document_tombstones where document_id = $1",
        [documentId],
      );
      if (!existing.rows[0]) throw new DocumentNotFoundError();
      return rowToTombstone(existing.rows[0]);
    });
  }

  async issueDownloadTicket(
    context: AccessContext,
    documentId: string,
    revisionId: string,
    expiresInMinutes: number,
  ): Promise<DocumentDownloadTicket> {
    assertPositiveInteger(expiresInMinutes, "expiresInMinutes");
    return scoped(context, async (client) => {
      const result = await throwNotFoundOnFk(() =>
        client.query<{ ticket_id: string; document_id: string; revision_id: string; expires_at: Date }>(
          `insert into app.research_document_download_tickets
             (ticket_id, document_id, revision_id, tenant_id, owner_principal_id, expires_at)
           select $1, $2, $3, $4, $5, now() + make_interval(mins => $6)
           where ${notTombstoned("$2")}
           returning ticket_id, document_id, revision_id, expires_at`,
          [idOf("ticket"), documentId, revisionId, context.tenantId, context.principalId, expiresInMinutes],
        ),
      );
      const row = result.rows[0];
      if (!row) throw new DocumentNotFoundError();
      return {
        ticketId: row.ticket_id,
        documentId: row.document_id,
        revisionId: row.revision_id,
        expiresAt: row.expires_at.toISOString(),
      };
    });
  }

  async redeemDownloadTicket(context: AccessContext, ticketId: string): Promise<Buffer | null> {
    return scoped(context, async (client) => {
      const redemption = await client.query<{ revision_id: string }>(
        `update app.research_document_download_tickets t set redeemed_at = now()
         where t.ticket_id = $1 and t.redeemed_at is null and t.expires_at > now() and ${notTombstoned("t.document_id")}
         returning t.revision_id`,
        [ticketId],
      );
      const revisionId = redemption.rows[0]?.revision_id;
      if (!revisionId) return null;
      return readRevisionObject(client, "revision_id = $1", [revisionId]);
    });
  }

  async getRevisionBytes(context: AccessContext, documentId: string, revisionId: string): Promise<Buffer | null> {
    return scoped(context, (client) => readRevisionObject(client, "document_id = $1 and revision_id = $2", [documentId, revisionId]));
  }
}

export function parseByteLength(value: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`artifact_byte_length is not a safe non-negative integer: ${value}`);
  }
  return parsed;
}

export class DocumentNotFoundError extends Error {
  constructor() {
    super("document not found");
  }
}

function isForeignKeyViolation(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: string }).code === POSTGRES_FOREIGN_KEY_VIOLATION;
}

async function insertRevision(
  client: RuntimeClient,
  context: AccessContext,
  documentId: string,
  revision: NewDocumentRevisionInput,
  objectRef: { key: string; sha256: string; byteLength: number; contentType: string },
): Promise<ResearchDocumentRevision> {
  const result = await throwNotFoundOnFk(() =>
    client.query<RevisionRow>(
      `insert into app.research_document_revisions
         (revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id, ${REV_OBJ_COLS})
       select $1, $2, $3, $4, $5, $6, $7, $8, $9 where ${notTombstoned("$2")}
       returning ${REV_COLS}`,
      [
        idOf("revision"),
        documentId,
        context.tenantId,
        context.principalId,
        revision.sourceArtifactId,
        objectRef.key,
        objectRef.sha256,
        objectRef.byteLength,
        objectRef.contentType,
      ],
    ),
  );
  if (!result.rows[0]) throw new DocumentNotFoundError();
  return rowToRevision(result.rows[0]);
}

const rowToDocument = (r: DocumentRow): ResearchDocument => ({ documentId: r.document_id, tenantId: r.tenant_id, ownerPrincipalId: r.owner_principal_id, createdAt: r.created_at.toISOString() });
const rowToRevision = (r: RevisionRow): ResearchDocumentRevision => ({
  revisionId: r.revision_id, documentId: r.document_id, tenantId: r.tenant_id, ownerPrincipalId: r.owner_principal_id,
  sourceArtifactId: r.source_artifact_id, artifactSha256: r.artifact_sha256, artifactByteLength: parseByteLength(r.artifact_byte_length),
  artifactContentType: r.artifact_content_type, createdAt: r.created_at.toISOString(),
});
const rowToTombstone = (r: TombstoneRow): DocumentTombstone => ({ tombstoneId: r.tombstone_id, documentId: r.document_id, createdAt: r.created_at.toISOString() });
