/**
 * #373 / #44: one document's revisions, owner-scoped. `getDocument` returning
 * `null` covers both "does not exist" and "exists but belongs to someone
 * else or is tombstoned" identically (RLS + tombstone filter), which is the
 * non-enumerating property the issue requires.
 *
 * When the latest revision is a `text/html` Xiaohongshu card artifact (#44),
 * renders a sandboxed 1080x1440 (3:4 portrait) inline preview alongside the
 * revision metadata.
 */

import { notFound, redirect } from "next/navigation";
import { decodeRouteParam, formatCell, formatNumberMagnitude, formatShortSha } from "@/client/format";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresDocumentsRepository } from "@/server/documents";

export const dynamic = "force-dynamic";

const repository = new PostgresDocumentsRepository();

export default async function DocumentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");

  const { id } = await params;
  const document = await repository.getDocument(principal.context, decodeRouteParam(id));
  if (!document) notFound();

  const revisions = await repository.listRevisions(principal.context, document.documentId);
  const latestRevision = revisions[0] ?? null;
  let latestHtmlPreview: string | null = null;
  if (
    latestRevision &&
    latestRevision.artifactContentType.toLowerCase().startsWith("text/html") &&
    typeof repository.getRevisionBytes === "function"
  ) {
    try {
      const bytes = await repository.getRevisionBytes(
        principal.context,
        document.documentId,
        latestRevision.revisionId,
      );
      if (bytes) {
        latestHtmlPreview = bytes.toString("utf8");
      }
    } catch {
      latestHtmlPreview = null;
    }
  }

  return (
    <section aria-labelledby="document-heading" className="space-y-6">
      <h1 id="document-heading" className="text-2xl font-bold tracking-tight">
        Document
      </h1>
      <p className="text-xs text-gray-500">
        <span data-evidence={document.documentId}>{document.documentId}</span> — created {document.createdAt}
      </p>

      {latestHtmlPreview && (
        <div
          data-testid="xhs-card-html-preview"
          className="rounded-xl border border-border bg-card p-4 space-y-3"
        >
          <div className="flex items-center justify-between text-xs text-gray-400">
            <span className="font-semibold uppercase tracking-wider">
              Xiaohongshu Card Preview (1080 × 1440 · 3:4 Portrait HTML)
            </span>
            <span className="font-mono">{latestRevision?.sourceArtifactId}</span>
          </div>
          <div className="overflow-auto rounded-lg border border-border bg-black/20 p-2">
            <iframe
              sandbox=""
              srcDoc={latestHtmlPreview}
              title={`Xiaohongshu card preview for ${document.documentId}`}
              width={1080}
              height={1440}
              className="mx-auto block rounded bg-white"
            />
          </div>
        </div>
      )}

      {revisions.length === 0 ? (
        <p role="status" className="text-sm text-gray-500">
          No revisions yet.
        </p>
      ) : (
        <ul className="space-y-3">
          {revisions.map((revision) => (
            <li key={revision.revisionId} className="rounded-lg border border-border bg-card p-4 text-sm">
              <span className="text-xs uppercase tracking-wider text-gray-500" data-evidence={revision.sourceArtifactId}>
                {revision.sourceArtifactId}
              </span>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-400">
                <dt>sha256</dt>
                <dd className="truncate font-mono" title={revision.artifactSha256}>
                  {formatShortSha(revision.artifactSha256)}
                </dd>
                <dt>bytes</dt>
                <dd className="font-mono tabular-nums" title={String(revision.artifactByteLength)}>
                  {formatCell(formatNumberMagnitude(revision.artifactByteLength))}
                </dd>
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
