"use server";

/**
 * #396: server actions backing /research/conversations. Every action
 * re-derives AccessContext from the verified session itself (never trusts
 * anything passed from the client) and redirects to /login if there is
 * none — the same rule every other #371 route follows.
 */

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresConversationsRepository } from "@/server/conversations";

const repository = new PostgresConversationsRepository();

export async function createConversationAction(): Promise<void> {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fconversations");
  const conversation = await repository.createConversation(principal.context);
  revalidatePath("/research/conversations");
  redirect(`/research/conversations/${encodeURIComponent(conversation.conversationId)}`);
}

export async function appendUserMessageAction(conversationId: string, formData: FormData): Promise<void> {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fconversations");
  const content = String(formData.get("content") ?? "");
  if (content.trim().length === 0) return;
  // outcome is null: this is a user prompt, not a processed reply — #396
  // does not implement intent extraction/response generation (#46's scope).
  await repository.appendMessage(principal.context, conversationId, "user", content, null);
  revalidatePath(`/research/conversations/${conversationId}`);
}
