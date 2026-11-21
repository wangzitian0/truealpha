import Link from "next/link";
import { api } from "@/client/api";
import { decodeRouteParam } from "@/client/format";
import { loadAuthed } from "../../../_load";
import { ReadStateNotice } from "@/components/read-state";

export const dynamic = "force-dynamic";

export default async function DocumentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { data: detail, error } = await loadAuthed("/research/library", (opts) => api.getDocument(decodeRouteParam(id), opts), { notFoundOn404: true });
  if (!detail) return error ? <ReadStateNotice state={{ kind: "error", message: error }} /> : <p className="text-sm text-rose-400">Document unavailable.</p>;
  const revs = detail.revisions ?? [];
  const isHtml = revs[revs.length - 1]?.artifact_content_type.toLowerCase().includes("text/html") ?? false;

  return (
    <section aria-labelledby="doc-detail-heading" className="space-y-6">
      <div>
        <Link href="/research/library" className="text-xs text-accent hover:underline">← Back to library</Link>
        <h1 id="doc-detail-heading" className="mt-1 font-mono text-xl font-bold text-white">{detail.document.document_id}</h1>
        <p className="text-xs text-gray-500">Created {detail.document.created_at} · {revs.length} revision(s)</p>
      </div>
      {detail.latest_content != null && (isHtml ? (
        <iframe sandbox="" srcDoc={detail.latest_content} title="Research Card HTML Preview" className="h-[720px] w-full rounded-xl border border-border bg-white" />
      ) : (
        <pre className="overflow-x-auto whitespace-pre-wrap rounded-xl border border-border bg-card p-4 font-mono text-xs text-gray-200">{detail.latest_content}</pre>
      ))}
      <div className="rounded-xl border border-border bg-card p-4">
        <h2 className="mb-2 text-sm font-semibold">Revisions</h2>
        <ul className="space-y-2 font-mono text-xs text-gray-400">
          {revs.map((rev) => (
            <li key={rev.revision_id}>{rev.revision_id} · {rev.source_artifact_id} · {rev.artifact_content_type} · {rev.artifact_byte_length} bytes</li>
          ))}
        </ul>
      </div>
    </section>
  );
}
