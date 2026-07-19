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
import { bucket, getDocumentArtifact, storeDocumentArtifact } from "@/server/documents/object-store";

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

/** Keyset cursor: `created_at` alone is not unique (two documents can share
 * a timestamp), so `documentId` breaks the tie — without it, pagination
 * could skip or repeat rows whenever a page boundary lands on a shared
 * `created_at`. */
export interface DocumentCursor {
  createdAt: string;
  documentId: string;
}

export interface DocumentListQuery {
  limit?: number;
  /** Exclusive cursor: pass the previous page's last row's cursor to continue. */
  before?: DocumentCursor;
}

export interface DocumentPage {
  documents: ResearchDocument[];
  nextBefore: DocumentCursor | null;
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

/** Rejects an empty/whitespace-only field before it ever reaches S3 or the
 * DB's own `length(...) > 0` CHECK — otherwise a blank `sourceArtifactId`/
 * `contentType` would still get written to object storage before the
 * (inevitable) DB insert failure, leaving an avoidable orphaned object. */
export function assertNonEmptyText(value: string, fieldName: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    throw new Error(`${fieldName} must not be empty`);
  }
  return trimmed;
}

// Mirrors truealpha_contracts.access._stable_coordinate exactly (the same
// function documents.py's _stable_id now delegates to) — sourceArtifactId
// is a caller-supplied stable lineage identifier (a #369/#372 report_id/
// card_id), so the TS adapter must reject the same inputs the Python DTO
// would, or the two sides silently disagree about what's a valid id.
const STABLE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;
const MUTABLE_TOKENS = new Set(["latest", "current", "default", "head"]);

export function assertStableId(value: string, fieldName: string): string {
  if (!STABLE_ID_PATTERN.test(value)) {
    throw new Error(`${fieldName} must be a stable identifier`);
  }
  const tokens = value.toLowerCase().split(/[._:/@+-]/).filter((token) => token.length > 0);
  if (tokens.some((token) => MUTABLE_TOKENS.has(token))) {
    throw new Error(`${fieldName} must name an immutable version`);
  }
  return value;
}

function validateNewRevision(revision: NewDocumentRevisionInput): NewDocumentRevisionInput {
  return {
    ...revision,
    // No trim here: a stable identifier with leading/trailing whitespace
    // should be REJECTED, not silently normalized — trimming first could
    // accept an input the Python DTOs' _stable_id would reject outright.
    sourceArtifactId: assertStableId(revision.sourceArtifactId, "sourceArtifactId"),
    contentType: assertNonEmptyText(revision.contentType, "contentType"),
  };
}

// `new Date(...)` alone accepts non-ISO/date-only/implementation-dependent
// strings, which contradicts the explicit ISO date-time check the rest of
// the codebase uses (e.g. contracts/strategyRun.ts's CUTOFF_PATTERN).
const ISO_DATE_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

/** Rejects a malformed cursor before it ever reaches `$1::timestamptz` — an
 * invalid string there would surface as a raw Postgres cast error instead
 * of a clear, deterministic one here. */
export function parseBeforeCursor(before: DocumentCursor | undefined): { createdAt: Date; documentId: string } | null {
  if (before === undefined) return null;
  if (!ISO_DATE_TIME_PATTERN.test(before.createdAt)) {
    throw new Error("before.createdAt must be a valid ISO datetime");
  }
  const createdAt = new Date(before.createdAt);
  if (Number.isNaN(createdAt.getTime())) {
    throw new Error("before.createdAt must be a valid ISO datetime");
  }
  // Same reasoning as validateNewRevision: no trim before validating a
  // stable identifier.
  const documentId = assertStableId(before.documentId, "before.documentId");
  return { createdAt, documentId };
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
           and (
             $1::timestamptz is null
             or (d.created_at, d.document_id) < ($1::timestamptz, $2)
           )
           order by d.created_at desc, d.document_id desc
           limit $3`,
          [before?.createdAt ?? null, before?.documentId ?? null, limit],
        );
        const documents = result.rows.map(rowToDocument);
        const last = documents[documents.length - 1];
        const nextBefore = documents.length === limit && last ? { createdAt: last.createdAt, documentId: last.documentId } : null;
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
           order by r.created_at asc, r.revision_id asc`,
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
    const validated = validateNewRevision(revision);
    const objectRef = await storeDocumentArtifact(context.principalId, validated.bytes, validated.contentType);
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
        const revisionRow = await insertRevision(client, context, documentId, validated, objectRef);
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
        const validated = validateNewRevision(revision);
        const objectRef = await storeDocumentArtifact(context.principalId, validated.bytes, validated.contentType);
        return insertRevision(client, context, documentId, validated, objectRef);
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
        // exists, but that row is not guaranteed to be this caller's own —
        // ON CONFLICT DO NOTHING resolves purely on document_id and skips
        // FK enforcement for the skipped row (see db/tests/documents_contract.sql's
        // "bob's forged tombstone on alice's document" case for a
        // reproduced proof). The SELECT below is scoped by this caller's
        // own RLS, so it correctly returns nothing when the existing row
        // belongs to someone else; treat that identically to "not found"
        // rather than crashing on an empty result. Only a caller
        // re-tombstoning their *own* already-deleted document sees the
        // (idempotent) existing row.
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
    // The S3 read happens AFTER this transaction commits and releases the
    // ticket row lock — holding a DB transaction open across network I/O
    // would needlessly extend lock contention under load.
    const objectRef = await withOwnerScopedRuntime(
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

        return {
          bucket: bucket(),
          key: revisionRow.object_key,
          sha256: revisionRow.artifact_sha256,
          byteLength: Number(revisionRow.artifact_byte_length),
          contentType: revisionRow.artifact_content_type,
        };
      },
    );
    if (!objectRef) return null;
    return getDocumentArtifact(objectRef);
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
