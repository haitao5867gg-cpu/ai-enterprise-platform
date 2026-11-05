import "server-only";
import { randomUUID } from "node:crypto";
import { converses } from "../apps";
import type { Conversation, ConversationDetail, Turn } from "../conversations";
import { MAX_MESSAGE } from "../conversations";
import { isRecord, type Run } from "../runs";
import {
  invalid,
  mockError,
  mockJson,
  noContent,
  readBody,
} from "./mock-assets";
import { mockAppSnapshot, mockAppTitle } from "./mock-apps";
import {
  mockClearRuns,
  mockConversationTurn,
  mockRetitleRuns,
  mockRunView,
} from "./mock-runs";

// docs/api/conversations.md, in memory. Everyone is the same person here, as with
// sign-in off.
type Stored = {
  conversation_id: string;
  app: string;
  title: string | null;
  draft: boolean;
  input: Record<string, unknown>;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  runs: string[];
};
const RETENTION_DAYS = 30;
const holder = globalThis as typeof globalThis & {
  aepMockConversations?: Map<string, Stored>;
};
const store = (holder.aepMockConversations ??= new Map());

const titleOf = (item: Stored) => item.title ?? "新对话";
function turns(item: Stored): { turn: Turn; run: Run }[] {
  return item.runs.flatMap((id, index) => {
    const run = mockRunView(id);
    if (!run) return [];
    const snapshot = mockAppSnapshot(item.app, undefined, item.draft);
    const reply =
      snapshot && "conversation" in snapshot.config
        ? (snapshot.config.conversation?.reply ?? "reply")
        : "reply";
    const result = isRecord(run.result) ? run.result : null;
    return [
      {
        run,
        turn: {
          turn: index + 1,
          run_id: id,
          message: String(run.input?.message ?? ""),
          reply:
            run.status === "done" && typeof result?.[reply] === "string"
              ? (result[reply] as string)
              : null,
          status: run.status,
          pending: run.pending,
          sources: run.sources ?? [],
          app_version: run.app_version ?? null,
          created_at: run.created_at ?? item.created_at,
          finished_at: run.finished_at ?? null,
        },
      },
    ];
  });
}
function view(item: Stored): Conversation {
  const all = turns(item);
  const last = all.at(-1)?.turn.status;
  return {
    conversation_id: item.conversation_id,
    app: item.app,
    app_title: mockAppTitle(item.app) ?? item.app,
    title: titleOf(item),
    draft: item.draft,
    input: item.input,
    created_by: item.created_by,
    created_at: item.created_at,
    updated_at: item.updated_at,
    expires_at: new Date(
      Date.parse(item.updated_at) + RETENTION_DAYS * 86_400_000,
    ).toISOString(),
    turn_count: all.length,
    busy: last === "running" || last === "waiting",
  };
}
const detail = (item: Stored): ConversationDetail => ({
  ...view(item),
  turns: turns(item).map(({ turn }) => turn),
});

export async function mockConversations(
  request: Request,
  path: string[],
): Promise<Response> {
  if (path.length === 1 && request.method === "GET") {
    const query = new URL(request.url).searchParams;
    const app = query.get("app");
    const draft = query.get("draft") === "true";
    const limit = Math.min(Math.max(Number(query.get("limit") ?? 50), 1), 200);
    return mockJson(
      [...store.values()]
        .filter((item) => (!app || item.app === app) && item.draft === draft)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
        .slice(0, limit)
        .map(view),
    );
  }
  if (path.length === 1 && request.method === "POST") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    if (typeof body.app !== "string") return mockError("应用不存在", 404);
    const draft = body.draft === true;
    const current = mockAppSnapshot(body.app);
    if (!current) return mockError("应用不存在", 404);
    const snapshot = draft ? mockAppSnapshot(body.app, undefined, true) : current;
    if (!snapshot) return mockError("没有草稿可以试", 409);
    if (!converses(snapshot.config))
      return mockError("这个应用没有开对话", 409);
    if (body.input !== undefined && !isRecord(body.input))
      return invalid([{ loc: ["body", "input"], msg: "input 要是对象" }]);
    if (
      body.title !== undefined &&
      (typeof body.title !== "string" ||
        !body.title.trim() ||
        body.title.length > 80)
    )
      return invalid([{ loc: ["body", "title"], msg: "名称 1–80 个字符" }]);
    const now = new Date().toISOString();
    const item: Stored = {
      conversation_id: randomUUID(),
      app: body.app,
      title: typeof body.title === "string" ? body.title.trim() : null,
      draft,
      input: isRecord(body.input) ? body.input : {},
      created_by:
        typeof body.requester === "string" && body.requester.trim()
          ? body.requester.trim()
          : null,
      created_at: now,
      updated_at: now,
      runs: [],
    };
    store.set(item.conversation_id, item);
    return mockJson(view(item), 201);
  }
  const item = store.get(path[1]);
  if (!item) return mockError("没有找到这个会话", 404);
  if (path.length === 2 && request.method === "GET")
    return mockJson(detail(item));
  if (path.length === 2 && request.method === "DELETE") {
    store.delete(item.conversation_id);
    mockClearRuns(item.runs);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (path.length === 2 && request.method === "PATCH") {
    if (
      typeof body.title !== "string" ||
      !body.title.trim() ||
      body.title.length > 80
    )
      return invalid([{ loc: ["body", "title"], msg: "名称 1–80 个字符" }]);
    item.title = body.title.trim();
    mockRetitleRuns(item.runs, item.title);
    return mockJson(view(item));
  }
  if (path[2] === "messages" && request.method === "POST") {
    const message =
      typeof body.message === "string" ? body.message.trim() : "";
    if (!message || message.length > MAX_MESSAGE)
      return invalid([
        { loc: ["body", "message"], msg: `消息 1–${MAX_MESSAGE} 个字符` },
      ]);
    if (view(item).busy) return mockError("上一条还没处理完", 409);
    // Like the backend: a conversation is named after its first message.
    item.title ??= message.slice(0, 30);
    const started = await mockConversationTurn(
      {
        app: item.app,
        input: { ...item.input, message },
        ...(item.draft ? { draft: true } : {}),
        ...(typeof body.requester === "string"
          ? { requester: body.requester }
          : item.created_by
            ? { requester: item.created_by }
            : {}),
      },
      {
        conversation_id: item.conversation_id,
        title: titleOf(item),
        turn: item.runs.length + 1,
      },
    );
    if (started instanceof Response) return started;
    item.runs.push(started.run_id);
    item.updated_at = new Date().toISOString();
    return mockJson({ ...started, turn: item.runs.length }, 201);
  }
  return mockError("Not found", 404);
}
