/**
 * #373: real persistence. Lists only the authenticated principal's own
 * documents (RLS-enforced, not filtered here; tombstoned documents are
 * excluded by the repository query itself). Creation here is a plain-text
 * "note" document — proves the full create/store/list/tombstone path end
 * to end; #369/#372 report/card artifacts are #46/future scope for what
 * gets persisted as a document.
 */

import Link from "next/link";
import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresDocumentsRepository } from "@/server/documents";
import { createNoteDocumentAction, tombstoneDocumentAction } from "@/server/documents-actions";

export const dynamic = "force-dynamic";

const repository = new PostgresDocumentsRepository();

export default async function LibraryPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");

  const page = await repository.listDocuments(principal.context, {});

  return (
    <section aria-labelledby="library-heading" className="space-y-6">
      <div>
        <h1 id="library-heading" className="text-2xl font-bold tracking-tight">
          Library
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Owner-scoped, RLS-isolated (#373). Reports/cards persisting here as documents is
          separate scope — this page proves create/store/list/tombstone end to end.
        </p>
      </div>

      <form action={createNoteDocumentAction} className="flex gap-2">
        <input
          type="text"
          name="content"
          required
          placeholder="Quick note…"
          className="flex-1 rounded-md border border-border bg-card px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white"
        >
          Save
        </button>
      </form>

      {page.documents.length === 0 ? (
        <p role="status" className="text-sm text-gray-500">
          No documents yet.
        </p>
      ) : (
        <ul className="space-y-2">
          {page.documents.map((document) => (
            <li
              key={document.documentId}
              className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3 text-sm"
            >
              <Link
                href={`/research/library/${encodeURIComponent(document.documentId)}`}
                className="flex-1 text-gray-300 hover:text-white"
              >
                {document.documentId} — {document.createdAt}
              </Link>
              <form action={tombstoneDocumentAction.bind(null, document.documentId)}>
                <button type="submit" className="text-xs text-gray-500 hover:text-red-400">
                  Delete
                </button>
              </form>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
