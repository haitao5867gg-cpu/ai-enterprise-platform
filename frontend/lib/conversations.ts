import type { RunSource } from "./knowledge";
import {
  ApiError,
  isRecord,
  requestJson,
  type Approval,
  type RunStatus,
} from "./runs";

// docs/api/conversations.md is the source of truth for these wire types.
export type Conversation = {
  conversation_id: string;
  app: string;
  app_title: string;
  title: string;
  // A builder's debug chat on the app page: every turn runs the draft.
  draft: boolean;
  input: Record<string, unknown>;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  turn_count: number;
  // The last turn is running or waits for a person.
  busy: boolean;
};
export type Turn = {
  turn: number;
  run_id: string;
  message: string;
  reply: string | null;
  status: RunStatus;
  pending: Approval | null;
  sources: RunSource[];
  app_version: number | null;
  created_at: string;
  finished_at: string | null;
};
export type ConversationDetail = Conversation & { turns: Turn[] };
export const MAX_MESSAGE = 8000;

function isConversation(value: unknown): value is Conversation {
  return (
    isRecord(value) &&
    typeof value.conversation_id === "string" &&
    typeof value.app === "string" &&
    typeof value.title === "string" &&
    typeof value.turn_count === "number" &&
    typeof value.busy === "boolean"
  );
}
function readConversation(value: unknown): Conversation {
  if (!isConversation(value)) throw new ApiError("会话格式不正确，请稍后重试。");
  return value;
}

export async function listConversations(
  app: string,
  draft: boolean,
  signal?: AbortSignal,
): Promise<Conversation[]> {
  const value = await requestJson(
    `/conversations?app=${encodeURIComponent(app)}&draft=${draft}&limit=50`,
    { signal },
  );
  if (!Array.isArray(value) || !value.every(isConversation))
    throw new ApiError("会话列表格式不正确，请稍后重试。");
  return value;
}
export async function openConversation(input: {
  app: string;
  draft?: boolean;
  input?: Record<string, unknown>;
  title?: string;
  requester?: string;
}): Promise<Conversation> {
  return readConversation(
    await requestJson("/conversations", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
}
export async function getConversation(
  id: string,
  signal?: AbortSignal,
): Promise<ConversationDetail> {
  const value = await requestJson(`/conversations/${encodeURIComponent(id)}`, {
    signal,
  });
  if (!isConversation(value) || !Array.isArray((value as ConversationDetail).turns))
    throw new ApiError("会话格式不正确，请稍后重试。");
  return value as ConversationDetail;
}
export const sendMessage = (id: string, message: string, requester?: string) =>
  requestJson(`/conversations/${encodeURIComponent(id)}/messages`, {
    method: "POST",
    body: JSON.stringify({ message, ...(requester ? { requester } : {}) }),
  }) as Promise<{ run_id: string; status: RunStatus; turn: number }>;
export const renameConversation = async (id: string, title: string) =>
  readConversation(
    await requestJson(`/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  );
export const deleteConversation = (id: string) =>
  requestJson(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
