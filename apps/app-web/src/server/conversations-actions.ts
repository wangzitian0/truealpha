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
import { answerUserPrompt } from "@/server/conversations-answer";

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
  // outcome is null: this is a user prompt, not a processed reply.
  await repository.appendMessage(principal.context, conversationId, "user", content, null);
  // The first reply loop (#46 v0): deterministic intent over the governed mart
  // readers, honest `unsupported` otherwise. The answer layer decides;
  // storage above stays pure. A reply failure must not lose the user's
  // prompt — it is already stored; the thread simply shows no reply yet.
  const answer = await answerUserPrompt(content);
  await repository.appendMessage(principal.context, conversationId, "assistant", answer.content, answer.outcome);
  revalidatePath(`/research/conversations/${conversationId}`);
}
