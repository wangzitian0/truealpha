/**
 * #373: one document's revisions, owner-scoped. `getDocument` returning
 * `null` covers both "does not exist" and "exists but belongs to someone
 * else or is tombstoned" identically (RLS + tombstone filter), which is the
 * non-enumerating property the issue requires.
 */

import { notFound, redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresDocumentsRepository } from "@/server/documents";

export const dynamic = "force-dynamic";

const repository = new PostgresDocumentsRepository();

export default async function DocumentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");

  const { id } = await params;
  // Next.js does not decode a dynamic route segment for us — the id here
  // is still percent-encoded (colons in document_id round-trip through
  // encodeURIComponent on the list page's link), so every lookup would
  // otherwise 404 for any id containing a reserved URI character.
  const document = await repository.getDocument(principal.context, decodeURIComponent(id));
  if (!document) notFound();

  const revisions = await repository.listRevisions(principal.context, document.documentId);

  return (
    <section aria-labelledby="document-heading" className="space-y-6">
      <h1 id="document-heading" className="text-2xl font-bold tracking-tight">
        Document
      </h1>
      <p className="text-xs text-gray-500">
        {document.documentId} — created {document.createdAt}
      </p>

      {revisions.length === 0 ? (
        <p role="status" className="text-sm text-gray-500">
          No revisions yet.
        </p>
      ) : (
        <ul className="space-y-3">
          {revisions.map((revision) => (
            <li key={revision.revisionId} className="rounded-lg border border-border bg-card p-4 text-sm">
              <span className="text-xs uppercase tracking-wider text-gray-500">{revision.sourceArtifactId}</span>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-400">
                <dt>sha256</dt>
                <dd className="truncate font-mono">{revision.artifactSha256}</dd>
                <dt>bytes</dt>
                <dd>{revision.artifactByteLength}</dd>
                <dt>content type</dt>
                <dd>{revision.artifactContentType}</dd>
                <dt>created</dt>
                <dd>{revision.createdAt}</dd>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
