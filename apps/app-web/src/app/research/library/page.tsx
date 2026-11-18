import Link from "next/link";
import { api } from "@/client/api";
import { CreateDocumentForm, DeleteDocumentButton } from "@/components/document-actions";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function LibraryPage({ searchParams }: { searchParams: Promise<{ before_created_at?: string; before_document_id?: string }> }) {
  const sp = await searchParams;
  const { data, error } = await loadAuthed("/research/library", (opts) =>
    api.listDocuments({ before_created_at: sp.before_created_at, before_document_id: sp.before_document_id }, opts),
  );
  const docs = data?.documents ?? [];

  return (
    <section aria-labelledby="library-heading" className="space-y-6">
      <div>
        <h1 id="library-heading" className="text-2xl font-bold tracking-tight">Research Library</h1>
        <p className="mt-1 text-sm text-gray-400">Owner-isolated notes &amp; archived Xiaohongshu HTML cards via <code className="text-accent">/api/v1/library/documents</code>.</p>
      </div>
      <CreateDocumentForm />
      {error && <p className="text-sm text-rose-400">{error}</p>}
      {docs.length === 0 ? (
        <p className="text-sm text-gray-400">No documents saved yet.</p>
      ) : (
        <ul className="divide-y divide-border rounded-xl border border-border bg-card">
          {docs.map((doc) => (
            <li key={doc.document_id} className="flex items-center justify-between px-4 py-3 text-sm">
              <div>
                <Link href={`/research/library/${encodeURIComponent(doc.document_id)}`} className="font-mono text-accent hover:underline">{doc.document_id}</Link>
                <div className="text-xs text-gray-500">Created {doc.created_at}</div>
              </div>
              <DeleteDocumentButton documentId={doc.document_id} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
