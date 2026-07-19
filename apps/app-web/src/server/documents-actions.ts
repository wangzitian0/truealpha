"use server";

/**
 * #373: server actions backing /research/library. Every action re-derives
 * AccessContext from the verified session itself (never trusts anything
 * passed from the client) and redirects to /login if there is none — the
 * same rule every other #371 route follows.
 */

import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresDocumentsRepository } from "@/server/documents";

const repository = new PostgresDocumentsRepository();

export async function createNoteDocumentAction(formData: FormData): Promise<void> {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");
  const content = String(formData.get("content") ?? "");
  if (content.trim().length === 0) return;
  const { document } = await repository.createDocument(principal.context, {
    sourceArtifactId: `note:${randomUUID()}`,
    bytes: Buffer.from(content, "utf-8"),
    contentType: "text/plain",
  });
  revalidatePath("/research/library");
  redirect(`/research/library/${encodeURIComponent(document.documentId)}`);
}

export async function tombstoneDocumentAction(documentId: string): Promise<void> {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Flibrary");
  await repository.tombstoneDocument(principal.context, documentId);
  revalidatePath("/research/library");
}
