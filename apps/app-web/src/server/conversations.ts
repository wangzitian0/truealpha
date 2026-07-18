/**
 * #396: owner-scoped conversation persistence — the TypeScript adapter over
 * migration 0030's `app.conversations`/`app.conversation_messages`/
 * `app.clarification_tokens`/`app.research_gap_requests`. Structurally
 * mirrors `truealpha_contracts.conversations` (Python); this file performs
 * no intent extraction, no query selection, and computes nothing — it is
 * pure storage over already-decided outcomes, same boundary #370's mart
 * adapter holds for factor values (init.md Section 1, rule 2).
 *
 * Every method requires an `AccessContext` and reads/writes only through
 * `withOwnerScopedRuntime` (db.ts), so RLS — not this file — is what
 * actually prevents cross-owner access. A guessed/foreign/deleted ID simply
 * returns `null`/empty, identically to a truly nonexistent one (RLS makes
 * the two indistinguishable at the SQL level, which is the non-enumerating
 * property #396 requires).
 */

import { randomUUID } from "node:crypto";
import type { AccessContext } from "@/contracts/strategyRun";
import { withOwnerScopedRuntime } from "@/server/auth/db";
import type { QueryExecutor } from "@/server/auth/credentials";

export const CONVERSATION_OUTCOMES = [
  "result",
  "clarification_required",
  "unavailable",
  "unsupported",
  "denied",
  "rate_limited",
  "invalid",
] as const;
export type ConversationOutcome = (typeof CONVERSATION_OUTCOMES)[number];

export type MessageRole = "user" | "assistant";

export interface Conversation {
  conversationId: string;
  tenantId: string;
  ownerPrincipalId: string;
  createdAt: string;
}

export interface ConversationMessage {
  messageId: string;
  conversationId: string;
  tenantId: string;
  ownerPrincipalId: string;
  role: MessageRole;
  content: string;
  /** `null` for a user prompt — outcome is assistant-side semantics, set
   * once #46's orchestration resolves one of #225's typed states. */
  outcome: ConversationOutcome | null;
  createdAt: string;
}

export interface ClarificationToken {
  tokenId: string;
  conversationId: string;
  originatingMessageId: string;
  requestedFields: readonly string[];
  candidateChoices: readonly string[];
  expiresAt: string;
}

export interface NewGapRequest {
  conversationId: string | null;
  promptText: string;
}

/** Rejects an empty/whitespace-only prompt or message before it ever reaches a
 * query — the repository's Postgres methods assume this has already run. */
export function assertNonEmptyText(value: string, fieldName: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    throw new Error(`${fieldName} must not be empty`);
  }
  return trimmed;
}

export interface ConversationsRepository {
  listConversations(context: AccessContext): Promise<Conversation[]>;
  getConversation(context: AccessContext, conversationId: string): Promise<Conversation | null>;
  createConversation(context: AccessContext): Promise<Conversation>;
  listMessages(context: AccessContext, conversationId: string): Promise<ConversationMessage[]>;
  appendMessage(
    context: AccessContext,
    conversationId: string,
    role: MessageRole,
    content: string,
    outcome: ConversationOutcome | null,
  ): Promise<ConversationMessage>;
  issueClarificationToken(
    context: AccessContext,
    conversationId: string,
    originatingMessageId: string,
    requestedFields: readonly string[],
    candidateChoices: readonly string[],
    expiresInMinutes: number,
  ): Promise<ClarificationToken>;
  /** Atomic conditional redemption. Returns `false` for a missing, already-
   * redeemed, expired, or cross-owner token — all indistinguishable, by
   * design (#396 acceptance: a stale/replayed token must not leak whether
   * it existed, was redeemed, or expired). */
  redeemClarificationToken(context: AccessContext, tokenId: string): Promise<boolean>;
  /** Only ever called when the caller has already obtained explicit user
   * consent — there is no "declined" state to persist (#225). */
  recordGapRequest(context: AccessContext, request: NewGapRequest): Promise<void>;
}

function conversationIdOf(): string {
  return `conversation:${randomUUID()}`;
}

function messageIdOf(): string {
  return `message:${randomUUID()}`;
}

function tokenIdOf(): string {
  return `token:${randomUUID()}`;
}

function gapRequestIdOf(): string {
  return `gap:${randomUUID()}`;
}

export class PostgresConversationsRepository implements ConversationsRepository {
  async listConversations(context: AccessContext): Promise<Conversation[]> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<{
          conversation_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `select conversation_id, tenant_id, owner_principal_id, created_at
           from app.conversations
           order by created_at desc`,
        );
        return result.rows.map(rowToConversation);
      },
    );
  }

  async getConversation(context: AccessContext, conversationId: string): Promise<Conversation | null> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<{
          conversation_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `select conversation_id, tenant_id, owner_principal_id, created_at
           from app.conversations
           where conversation_id = $1`,
          [conversationId],
        );
        const row = result.rows[0];
        return row ? rowToConversation(row) : null;
      },
    );
  }

  async createConversation(context: AccessContext): Promise<Conversation> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const conversationId = conversationIdOf();
        const result = await client.query<{
          conversation_id: string;
          tenant_id: string;
          owner_principal_id: string;
          created_at: Date;
        }>(
          `insert into app.conversations (conversation_id, tenant_id, owner_principal_id)
           values ($1, $2, $3)
           returning conversation_id, tenant_id, owner_principal_id, created_at`,
          [conversationId, context.tenantId, context.principalId],
        );
        return rowToConversation(result.rows[0]);
      },
    );
  }

  async listMessages(context: AccessContext, conversationId: string): Promise<ConversationMessage[]> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query<{
          message_id: string;
          conversation_id: string;
          tenant_id: string;
          owner_principal_id: string;
          role: MessageRole;
          content: string;
          outcome: ConversationOutcome | null;
          created_at: Date;
        }>(
          `select message_id, conversation_id, tenant_id, owner_principal_id, role, content, outcome, created_at
           from app.conversation_messages
           where conversation_id = $1
           order by created_at asc`,
          [conversationId],
        );
        return result.rows.map(rowToMessage);
      },
    );
  }

  async appendMessage(
    context: AccessContext,
    conversationId: string,
    role: MessageRole,
    content: string,
    outcome: ConversationOutcome | null,
  ): Promise<ConversationMessage> {
    const validContent = assertNonEmptyText(content, "content");
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const messageId = messageIdOf();
        const result = await client.query<{
          message_id: string;
          conversation_id: string;
          tenant_id: string;
          owner_principal_id: string;
          role: MessageRole;
          content: string;
          outcome: ConversationOutcome | null;
          created_at: Date;
        }>(
          `insert into app.conversation_messages
             (message_id, conversation_id, tenant_id, owner_principal_id, role, content, outcome)
           values ($1, $2, $3, $4, $5, $6, $7)
           returning message_id, conversation_id, tenant_id, owner_principal_id, role, content, outcome, created_at`,
          [messageId, conversationId, context.tenantId, context.principalId, role, validContent, outcome],
        );
        return rowToMessage(result.rows[0]);
      },
    );
  }

  async issueClarificationToken(
    context: AccessContext,
    conversationId: string,
    originatingMessageId: string,
    requestedFields: readonly string[],
    candidateChoices: readonly string[],
    expiresInMinutes: number,
  ): Promise<ClarificationToken> {
    if (requestedFields.length === 0) {
      throw new Error("requestedFields must not be empty");
    }
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const tokenId = tokenIdOf();
        const result = await client.query<{
          token_id: string;
          conversation_id: string;
          originating_message_id: string;
          requested_fields: string[];
          candidate_choices: string[];
          expires_at: Date;
        }>(
          `insert into app.clarification_tokens
             (token_id, conversation_id, tenant_id, owner_principal_id, originating_message_id,
              requested_fields, candidate_choices, expires_at)
           values ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(mins => $8))
           returning token_id, conversation_id, originating_message_id, requested_fields, candidate_choices, expires_at`,
          [
            tokenId,
            conversationId,
            context.tenantId,
            context.principalId,
            originatingMessageId,
            requestedFields,
            candidateChoices,
            expiresInMinutes,
          ],
        );
        const row = result.rows[0];
        return {
          tokenId: row.token_id,
          conversationId: row.conversation_id,
          originatingMessageId: row.originating_message_id,
          requestedFields: row.requested_fields,
          candidateChoices: row.candidate_choices,
          expiresAt: row.expires_at.toISOString(),
        };
      },
    );
  }

  async redeemClarificationToken(context: AccessContext, tokenId: string): Promise<boolean> {
    return withOwnerScopedRuntime(
      { tenantId: context.tenantId, principalId: context.principalId },
      async (client) => {
        const result = await client.query(
          `update app.clarification_tokens
           set redeemed_at = now()
           where token_id = $1 and redeemed_at is null and expires_at > now()`,
          [tokenId],
        );
        return (result.rowCount ?? 0) > 0;
      },
    );
  }

  async recordGapRequest(context: AccessContext, request: NewGapRequest): Promise<void> {
    const promptText = assertNonEmptyText(request.promptText, "promptText");
    await withOwnerScopedRuntime({ tenantId: context.tenantId, principalId: context.principalId }, async (client) => {
      await client.query(
        `insert into app.research_gap_requests (gap_request_id, tenant_id, owner_principal_id, conversation_id, prompt_text)
         values ($1, $2, $3, $4, $5)`,
        [gapRequestIdOf(), context.tenantId, context.principalId, request.conversationId, promptText],
      );
    });
  }
}

function rowToConversation(row: {
  conversation_id: string;
  tenant_id: string;
  owner_principal_id: string;
  created_at: Date;
}): Conversation {
  return {
    conversationId: row.conversation_id,
    tenantId: row.tenant_id,
    ownerPrincipalId: row.owner_principal_id,
    createdAt: row.created_at.toISOString(),
  };
}

function rowToMessage(row: {
  message_id: string;
  conversation_id: string;
  tenant_id: string;
  owner_principal_id: string;
  role: MessageRole;
  content: string;
  outcome: ConversationOutcome | null;
  created_at: Date;
}): ConversationMessage {
  return {
    messageId: row.message_id,
    conversationId: row.conversation_id,
    tenantId: row.tenant_id,
    ownerPrincipalId: row.owner_principal_id,
    role: row.role,
    content: row.content,
    outcome: row.outcome,
    createdAt: row.created_at.toISOString(),
  };
}

// Re-exported so the QueryExecutor structural type is available to anything
// that wants to accept "a client already scoped to app_runtime" generically.
export type { QueryExecutor };
