"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/client/api";

export function CreateDocumentForm() {
  const router = useRouter();
  const [sourceId, setSourceId] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createDocument({ source_artifact_id: sourceId, content, content_type: "text/plain; charset=utf-8" });
      setSourceId("");
      setContent("");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to save note");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="rounded-xl border border-border bg-card p-5 space-y-3">
      <div className="flex flex-wrap gap-3">
        <input
          value={sourceId}
          onChange={(e) => setSourceId(e.target.value)}
          placeholder="Artifact ID (e.g. note:nvda-2026q3)"
          required
          className="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm text-white"
        />
        <button type="submit" disabled={busy} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
          {busy ? "Saving..." : "Save note"}
        </button>
      </div>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="Research note or markdown..."
        rows={3}
        required
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-white"
      />
      {error && <p className="text-xs text-rose-400">{error}</p>}
    </form>
  );
}

export function DeleteDocumentButton({ documentId }: { documentId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await api.deleteDocument(documentId);
          router.refresh();
        } finally {
          setBusy(false);
        }
      }}
      className="rounded border border-rose-500/40 px-2 py-1 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
    >
      Delete
    </button>
  );
}
