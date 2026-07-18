/**
 * #396: real persistence. Lists only the authenticated principal's own
 * conversations (RLS-enforced, not filtered here) and offers a form to
 * start a new one. No chat/reply generation — #46's scope.
 */

import Link from "next/link";
import { redirect } from "next/navigation";
import { getServerPrincipal } from "@/server/auth/request-context";
import { PostgresConversationsRepository } from "@/server/conversations";
import { createConversationAction } from "@/server/conversations-actions";

export const dynamic = "force-dynamic";

const repository = new PostgresConversationsRepository();

export default async function ConversationsPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fconversations");

  const conversations = await repository.listConversations(principal.context);

  return (
    <section aria-labelledby="conversations-heading" className="space-y-6">
      <div>
        <h1 id="conversations-heading" className="text-2xl font-bold tracking-tight">
          Conversations
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Owner-scoped, RLS-isolated (#396). Reply generation over typed research tools is
          separate scope (#46) — this page persists your own prompts only.
        </p>
      </div>

      <form action={createConversationAction}>
        <button
          type="submit"
          className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-gray-300 hover:border-accent hover:text-white"
        >
          Start a new conversation
        </button>
      </form>

      {conversations.length === 0 ? (
        <p role="status" className="text-sm text-gray-500">
          No conversations yet.
        </p>
      ) : (
        <ul className="space-y-2">
          {conversations.map((conversation) => (
            <li key={conversation.conversationId}>
              <Link
                href={`/research/conversations/${encodeURIComponent(conversation.conversationId)}`}
                className="block rounded-lg border border-border bg-card px-4 py-3 text-sm text-gray-300 hover:border-accent hover:text-white"
              >
                {conversation.conversationId} — {conversation.createdAt}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
