/**
 * #396: one conversation's messages, owner-scoped. `getConversation`
 * returning `null` covers both "does not exist" and "exists but belongs to
 * someone else" identically (RLS), which is the non-enumerating property
 * the issue requires — this page renders the same not-found state either
 * way, never distinguishing them.
 */

import { notFound, redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresConversationsRepository } from "@/server/conversations";
import { appendUserMessageAction } from "@/server/conversations-actions";

export const dynamic = "force-dynamic";

const repository = new PostgresConversationsRepository();

export default async function ConversationDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fconversations");

  const { id } = await params;
  const conversation = await repository.getConversation(principal.context, id);
  if (!conversation) notFound();

  const messages = await repository.listMessages(principal.context, conversation.conversationId);
  const appendMessage = appendUserMessageAction.bind(null, conversation.conversationId);

  return (
    <section aria-labelledby="conversation-heading" className="space-y-6">
      <h1 id="conversation-heading" className="text-2xl font-bold tracking-tight">
        Conversation
      </h1>

      {messages.length === 0 ? (
        <p role="status" className="text-sm text-gray-500">
          No messages yet.
        </p>
      ) : (
        <ul className="space-y-3">
          {messages.map((message) => (
            <li key={message.messageId} className="rounded-lg border border-border bg-card p-4 text-sm">
              <span className="text-xs uppercase tracking-wider text-gray-500">{message.role}</span>
              <p className="mt-1 text-gray-200">{message.content}</p>
            </li>
          ))}
        </ul>
      )}

      <form action={appendMessage} className="flex gap-2">
        <input
          type="text"
          name="content"
          required
          placeholder="Ask a research question…"
          className="flex-1 rounded-md border border-border bg-card px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white"
        >
          Send
        </button>
      </form>
    </section>
  );
}
