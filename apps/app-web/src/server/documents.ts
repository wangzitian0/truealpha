/**
 * #373: owner-scoped research document lifecycle — the TypeScript adapter
 * over migration 0031's `app.research_documents`/`research_document_revisions`/
 * `research_document_tombstones`/`research_document_download_tickets`.
 * Structurally mirrors `truealpha_contracts.documents` (Python)'s
 * `OwnedDocumentService` port; this file performs no rendering and no
 * authorization decision — it persists an already-rendered #369/#372
 * artifact's bytes as an opaque, content-addressed revision, same boundary
 * #396's conversations adapter holds for conversation storage (init.md
 * Section 1, rule 2).
 *
 * Every method requires an `AccessContext` and reads/writes only through
 * `withOwnerScopedRuntime` (db.ts), so RLS — not this file — is what
 * actually prevents cross-owner access. A tombstoned or foreign/guessed
 * document_id is treated identically to a nonexistent one everywhere below
 * (non-enumerating), matching #396's `ClarificationToken` precedent for
 * `redeemDownloadTicket`.
 */

import { randomUUID } from "node:crypto";
import type { AccessContext } from "@/contracts/strategyRun";
import { withOwnerScopedRuntime } from "@/server/auth/db";
import { getDocumentArtifact, storeDocumentArtifact } from "@/server/documents/object-store";

export interface ResearchDocument {
  documentId: string;
  tenantId: string;
  ownerPrincipalId: string;
  createdAt: string;
}

export interface ResearchDocumentRevision {
  revisionId: string;
  documentId: string;
  tenantId: string;
  ownerPrincipalId: string;
  sourceArtifactId: string;
  artifactSha256: string;
  artifactByteLength: number;
  artifactContentType: string;
  createdAt: string;
}

export interface DocumentTombstone {
  tombstoneId: string;
  documentId: string;
  createdAt: string;
}

export interface DocumentDownloadTicket {
  ticketId: string;
  documentId: string;
  revisionId: string;
  expiresAt: string;
}

export interface NewDocumentRevisionInput {
  /** Content-addressed id of the #369/#372 artifact this revision
   * serializes (e.g. `"report:<sha256>"`) — informational lineage only. */
  sourceArtifactId: string;
  bytes: Buffer;
  contentType: string;
}

export interface DocumentListQuery {
  limit?: number;
  /** Exclusive cursor: pass the previous page's oldest `createdAt` to continue. */
  before?: string;
}

export interface DocumentPage {
  documents: ResearchDocument[];
  nextBefore: string | null;
}

const DEFAULT_LIST_LIMIT = 50;
const MAX_LIST_LIMIT = 200;

function documentIdOf(): string {
  return `document:${randomUUID()}`;
}

function revisionIdOf(): string {
  return `revision:${randomUUID()}`;
}

function tombstoneIdOf(): string {
  return `tombstone:${randomUUID()}`;
}

function ticketIdOf(): string {
  return `ticket:${randomUUID()}`;
}

/** Rejects a non-positive or non-integer minute count before it ever reaches
 * `make_interval`/the DB's `expires_at > created_at` CHECK. */
export function assertPositiveInteger(value: number, fieldName: string): number {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${fieldName} must be a positive integer`);
  }
  return value;
}

/** Rejects a malformed cursor before it ever reaches `$1::timestamptz` — an
 * invalid string there would surface as a raw Postgres cast error instead
 * of a clear, deterministic one here. */
export function parseBeforeCursor(before: string | undefined): Date | null {
  if (before === undefined) return null;
  const parsed = new Date(before);
  if (Number.isNaN(parsed.getTime())) {
    throw new Error("before must be a valid ISO datetime");
  }
  return parsed;
}

export interface DocumentsRepository {
  listDocuments(context: AccessContext, query: DocumentListQuery): Promise<DocumentPage>;
  getDocument(context: AccessContext, documentId: string): Promise<ResearchDocument | null>;
  listRevisions(context: AccessContext, documentId: string): Promise<ResearchDocumentRevision[]>;
  createDocument(
    context: AccessContext,
    revision: NewDocumentRevisionInput,
  ): Promise<{ document: ResearchDocument; revision: ResearchDocumentRevision }>;
  appendRevision(
    context: AccessContext,
    documentId: string,
    revision: NewDocumentRevisionInput,
  ): Promise<ResearchDocumentRevision>;
  tombstoneDocument(context: AccessContext, documentId: string): Promise<DocumentTombstone>;
  issueDownloadTicket(
    context: AccessContext,
    documentId: string,
    revisionId: string,
    expiresInMinutes: number,
  ): Promise<DocumentDownloadTicket>;
  /** Returns `null` for a missing, already-redeemed, expired, cross-owner,
   * or tombstoned-document ticket — all indistinguishable, by design. */
  redeemDownloadTicket(context: AccessContext, ticketId: string): Promise<Buffer | null>;
}

export class PostgresDocumentsRepository implements DocumentsRepository {
  async listDocuments(context: AccessContext, query: DocumentListQuery): Promise<DocumentPage> {
    const limit = Math.min(Math.max(1, query.limit ?? DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT);
    const before = parseBeforeCursor(query.before);
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<{
          document_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at
           from app.research_documents d
           where not exists (
             select 1 from app.research_document_tombstones t where t.document_id = d.document_id
           )
           and ($1::timestamptz is null or d.created_at < $1)
           order by d.created_at desc
           limit $2`,
          [before, limit],
        );
        const documents = result.rows.map(rowToDocument);
        const nextBefore = documents.length === limit ? documents[documents.length - 1].createdAt : null;
        return { documents, nextBefore };
      },
    );
  }

  async getDocument(context: AccessContext, documentId: string): Promise<ResearchDocument | null> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<{
          document_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at
           from app.research_documents d
           where d.document_id = $1
             and not exists (
               select 1 from app.research_document_tombstones t where t.document_id = d.document_id
             )`,
          [documentId],
        );
        const row = result.rows[0];
        return row ? rowToDocument(row) : null;
      },
    );
  }

  async listRevisions(context: AccessContext, documentId: string): Promise<ResearchDocumentRevision[]> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<RevisionRow>(
          `select r.revision_id, r.document_id, r.tenant_id, r.owner_principal_id, r.source_artifact_id,
                  r.artifact_sha256, r.artifact_byte_length, r.artifact_content_type, r.created_at
           from app.research_document_revisions r
           where r.document_id = $1
             and not exists (
               select 1 from app.research_document_tombstones t where t.document_id = r.document_id
             )
           order by r.created_at asc`,
          [documentId],
        );
        return result.rows.map(rowToRevision);
      },
    );
  }

  async createDocument(
    context: AccessContext,
    revision: NewDocumentRevisionInput,
  ): Promise<{ document: ResearchDocument; revision: ResearchDocumentRevision }> {
    // No existence check possible here (createDocument always mints a new
    // document_id), so if the transaction below fails for an unrelated
    // reason, this object is orphaned in S3 with no cleanup path. Accepted
    // for this slice: content-addressed dedup means a retry with the same
    // bytes is a no-op write, not a growing leak, and the object carries no
    // ownership/RLS exposure on its own (object_key is never read back
    // without a matching, RLS-checked revision row). A real GC pass is out
    // of scope here — see #373's non-goals.
    const objectRef = await storeDocumentArtifact(context.principalId, revision.bytes, revision.contentType);
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const documentId = documentIdOf();
        const documentResult = await client.query<{
          document_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `insert into app.research_documents (document_id, tenant_id, owner_principal_id)
           values ($1, $2, $3)
           returning document_id, tenant_id, owner_principal_id, created_at`,
          [documentId, context.tenantId, context.principalId],
        );
        const revisionRow = await insertRevision(client, context, documentId, revision, objectRef);
        return { document: rowToDocument(documentResult.rows[0]), revision: revisionRow };
      },
    );
  }

  async appendRevision(
    context: AccessContext,
    documentId: string,
    revision: NewDocumentRevisionInput,
  ): Promise<ResearchDocumentRevision> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        // Check under RLS *before* writing to S3, so a typo'd/foreign/
        // already-tombstoned document_id fails without ever storing bytes
        // (fails-closed re-checked again by insertRevision's own guard,
        // which is the actual race-proof authority — this is a cheap
        // up-front rejection, not a substitute for it).
        const exists = await client.query(
          `select 1 from app.research_documents d
           where d.document_id = $1
             and not exists (
               select 1 from app.research_document_tombstones t where t.document_id = d.document_id
             )`,
          [documentId],
        );
        if (exists.rowCount === 0) {
          throw new DocumentNotFoundError();
        }
        const objectRef = await storeDocumentArtifact(context.principalId, revision.bytes, revision.contentType);
        return insertRevision(client, context, documentId, revision, objectRef);
      },
    );
  }

  async tombstoneDocument(context: AccessContext, documentId: string): Promise<DocumentTombstone> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        let inserted: { rows: { tombstone_id: string; document_id: string; created_at: Date }[] };
        try {
          inserted = await client.query<{ tombstone_id: string; document_id: string; created_at: Date }>(
            `insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id)
             values ($1, $2, $3, $4)
             on conflict (document_id) do nothing
             returning tombstone_id, document_id, created_at`,
            [tombstoneIdOf(), documentId, context.tenantId, context.principalId],
          );
        } catch (error) {
          // A genuinely nonexistent document_id fails the owner-scoped
          // composite FK before ON CONFLICT is even considered.
          if (isForeignKeyViolation(error)) throw new DocumentNotFoundError();
          throw error;
        }
        if (inserted.rows[0]) {
          return rowToTombstone(inserted.rows[0]);
        }
        // ON CONFLICT fired: a tombstone for this document_id already
        // exists. `unique (document_id)` alone is the conflict target, and
        // Postgres resolves ON CONFLICT DO NOTHING by skipping the row
        // before it is ever inserted — which means the composite FK
        // (an AFTER INSERT trigger on the *inserted* row) never runs for
        // it. So a forged tenant/owner in this INSERT's own values does
        // NOT get rejected by the FK here: it can be genuinely another
        // owner's tombstone on their own document_id, which RLS then
        // correctly hides from this caller's follow-up SELECT (verified
        // empirically against Postgres 16 — this is not a hypothetical).
        // Treat that identically to "not found" rather than crashing on an
        // empty result; only a caller re-tombstoning their *own*
        // already-deleted document sees the (idempotent) existing row.
        const existing = await client.query<{ tombstone_id: string; document_id: string; created_at: Date }>(
          `select tombstone_id, document_id, created_at
           from app.research_document_tombstones
           where document_id = $1`,
          [documentId],
        );
        if (!existing.rows[0]) {
          throw new DocumentNotFoundError();
        }
        return rowToTombstone(existing.rows[0]);
      },
    );
  }

  async issueDownloadTicket(
    context: AccessContext,
    documentId: string,
    revisionId: string,
    expiresInMinutes: number,
  ): Promise<DocumentDownloadTicket> {
    assertPositiveInteger(expiresInMinutes, "expiresInMinutes");
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        // Fails closed for a tombstoned document: a ticket issued after
        // deletion could never be redeemed anyway (redeemDownloadTicket
        // checks the same condition), so don't create the row at all.
        let result: { rows: { ticket_id: string; document_id: string; revision_id: string; expires_at: Date }[] };
        try {
          result = await client.query<{
            ticket_id: string;
            document_id: string;
            revision_id: string;
            expires_at: Date;
          }>(
            `insert into app.research_document_download_tickets
               (ticket_id, document_id, revision_id, tenant_id, owner_principal_id, expires_at)
             select $1, $2, $3, $4, $5, now() + make_interval(mins => $6)
             where not exists (
               select 1 from app.research_document_tombstones t where t.document_id = $2
             )
             returning ticket_id, document_id, revision_id, expires_at`,
            [ticketIdOf(), documentId, revisionId, context.tenantId, context.principalId, expiresInMinutes],
          );
        } catch (error) {
          if (isForeignKeyViolation(error)) throw new DocumentNotFoundError();
          throw error;
        }
        const row = result.rows[0];
        if (!row) {
          throw new DocumentNotFoundError();
        }
        return {
          ticketId: row.ticket_id,
          documentId: row.document_id,
          revisionId: row.revision_id,
          expiresAt: row.expires_at.toISOString(),
        };
      },
    );
  }

  async redeemDownloadTicket(context: AccessContext, ticketId: string): Promise<Buffer | null> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        // Re-authorized at redemption: expiry, single-use, and a
        // tombstoned-in-the-meantime document all fail this one WHERE
        // clause together — not a separate check after the fact.
        const redemption = await client.query<{ revision_id: string }>(
          `update app.research_document_download_tickets t
           set redeemed_at = now()
           where t.ticket_id = $1
             and t.redeemed_at is null
             and t.expires_at > now()
             and not exists (
               select 1 from app.research_document_tombstones ts where ts.document_id = t.document_id
             )
           returning t.revision_id`,
          [ticketId],
        );
        const revisionId = redemption.rows[0]?.revision_id;
        if (!revisionId) return null;

        const revisionResult = await client.query<{
          object_key: string;
          artifact_sha256: string;
          artifact_byte_length: number;
          artifact_content_type: string;
        }>(
          `select object_key, artifact_sha256, artifact_byte_length, artifact_content_type
           from app.research_document_revisions
           where revision_id = $1`,
          [revisionId],
        );
        const revisionRow = revisionResult.rows[0];
        if (!revisionRow) return null;

        return getDocumentArtifact({
          bucket: process.env.S3_BUCKET ?? "truealpha-raw",
          key: revisionRow.object_key,
          sha256: revisionRow.artifact_sha256,
          byteLength: Number(revisionRow.artifact_byte_length),
          contentType: revisionRow.artifact_content_type,
        });
      },
    );
  }
}

interface RevisionRow {
  revision_id: string;
  document_id: string;
  tenant_id: string;
  owner_principal_id: string;
  source_artifact_id: string;
  artifact_sha256: string;
  artifact_byte_length: number;
  artifact_content_type: string;
  created_at: Date;
}

/** Thrown for a missing, foreign, or tombstoned document/ticket — the same
 * outward shape regardless of which, so nothing enumerates which case it was. */
export class DocumentNotFoundError extends Error {
  constructor() {
    super("document not found");
  }
}

const POSTGRES_FOREIGN_KEY_VIOLATION = "23503";

function isForeignKeyViolation(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: string }).code === POSTGRES_FOREIGN_KEY_VIOLATION;
}

async function insertRevision(
  client: { query: <T>(text: string, params: unknown[]) => Promise<{ rows: T[] }> },
  context: AccessContext,
  documentId: string,
  revision: NewDocumentRevisionInput,
  objectRef: { key: string; sha256: string; byteLength: number; contentType: string },
): Promise<ResearchDocumentRevision> {
  const revisionId = revisionIdOf();
  let result: { rows: RevisionRow[] };
  try {
    result = await client.query<RevisionRow>(
      // INSERT ... SELECT (not VALUES) so a WHERE guard can fail the write
      // closed for a tombstoned document — the race-proof backstop behind
      // appendRevision's own up-front existence check.
      `insert into app.research_document_revisions
         (revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
          artifact_sha256, artifact_byte_length, artifact_content_type, object_key)
       select $1, $2, $3, $4, $5, $6, $7, $8, $9
       where not exists (
         select 1 from app.research_document_tombstones t where t.document_id = $2
       )
       returning revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
                 artifact_sha256, artifact_byte_length, artifact_content_type, created_at`,
      [
        revisionId,
        documentId,
        context.tenantId,
        context.principalId,
        revision.sourceArtifactId,
        objectRef.sha256,
        objectRef.byteLength,
        objectRef.contentType,
        objectRef.key,
      ],
    );
  } catch (error) {
    if (isForeignKeyViolation(error)) throw new DocumentNotFoundError();
    throw error;
  }
  if (!result.rows[0]) {
    throw new DocumentNotFoundError();
  }
  return rowToRevision(result.rows[0]);
}

function rowToDocument(row: {
  document_id: string;
  tenant_id: string;
  owner_principal_id: string;
  created_at: Date;
}): ResearchDocument {
  return {
    documentId: row.document_id,
    tenantId: row.tenant_id,
    ownerPrincipalId: row.owner_principal_id,
    createdAt: row.created_at.toISOString(),
  };
}

function rowToRevision(row: RevisionRow): ResearchDocumentRevision {
  return {
    revisionId: row.revision_id,
    documentId: row.document_id,
    tenantId: row.tenant_id,
    ownerPrincipalId: row.owner_principal_id,
    sourceArtifactId: row.source_artifact_id,
    artifactSha256: row.artifact_sha256,
    artifactByteLength: Number(row.artifact_byte_length),
    artifactContentType: row.artifact_content_type,
    createdAt: row.created_at.toISOString(),
  };
}

function rowToTombstone(row: { tombstone_id: string; document_id: string; created_at: Date }): DocumentTombstone {
  return {
    tombstoneId: row.tombstone_id,
    documentId: row.document_id,
    createdAt: row.created_at.toISOString(),
  };
}
