import "server-only";
import { randomUUID } from "node:crypto";
import type { User } from "../auth";
import type {
  AppNotification,
  NotificationEvent,
  Webhook,
  WebhookKind,
} from "../notifications";
import { EVENTS, KINDS } from "../notifications";
import { mockWho } from "./mock-auth";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";

// Mock of docs/api/notifications.md. Runs of the mock announce themselves through
// `announceMockRun`; with mock sign-in on (cookie aep_mock_auth) approvals go to
// approvers and admins, the rest to whoever started the run (by display name), and a
// run with no name to everyone. A webhook whose address contains "fail" fails its test.
type Stored = Omit<AppNotification, "read"> & {
  to: "approvers" | string | null;
  readBy: Set<string>;
};
type StoredHook = Webhook & { url: string };
const globalStore = globalThis as typeof globalThis & {
  aepMockNotices?: Stored[];
  aepMockHooks?: Map<string, StoredHook>;
};
const notices = (globalStore.aepMockNotices ??= []);
const hooks = (globalStore.aepMockHooks ??= new Map());

export function announceMockRun(run: {
  run_id: string;
  app: string;
  app_title?: string | null;
  status: string;
  title?: string | null;
  created_by?: string | null;
  error?: string | null;
  draft?: boolean;
  pending?: { kind?: string; prompt?: string } | null;
}) {
  const event = (
    { waiting: "run.waiting", done: "run.done", error: "run.error" } as const
  )[run.status as "waiting" | "done" | "error"];
  if (!event || run.draft) return;
  const app = run.app_title || run.app;
  const form = run.pending?.kind === "form";
  const about = run.title ?? "";
  const [title, body] =
    event === "run.waiting"
      ? [
          `「${app}」等你${form ? "填写" : "确认"}`,
          [about, run.pending?.prompt ?? ""].filter(Boolean).join("\n"),
        ]
      : event === "run.done"
        ? [`「${app}」运行完成`, about]
        : [
            `「${app}」运行失败`,
            [about, `原因：${run.error || "没有说明"}`]
              .filter(Boolean)
              .join("\n"),
          ];
  notices.push({
    id: notices.length + 1,
    event,
    run_id: run.run_id,
    app: run.app,
    title,
    body: body.slice(0, 500),
    link: `/runs/${run.run_id}`,
    created_at: new Date().toISOString(),
    to: event === "run.waiting" && !form ? "approvers" : run.created_by || null,
    readBy: new Set(),
  });
}

const reader = (user?: User) => user?.user_id ?? "local";
function visible(notice: Stored, on: boolean, user?: User) {
  if (!on || notice.to === null) return true;
  if (!user) return false;
  if (notice.to === "approvers")
    return user.roles.includes("approver") || user.roles.includes("admin");
  return notice.to === user.display_name;
}
const hookOut = ({ url, ...hook }: StoredHook): Webhook => ({
  ...hook,
  url_hint: `${new URL(url).protocol}//${new URL(url).hostname}/…${url.slice(-4)}`,
});
const validUrl = (value: unknown) =>
  typeof value === "string" && /^https?:\/\/\S+$/.test(value.trim());

export async function mockNotifications(request: Request, path: string[]) {
  const { on, user } = mockWho(request);
  if (on && !user) return mockError("请先登录", 401);
  const mine = notices.filter((n) => visible(n, on, user));
  const unread = () => mine.filter((n) => !n.readBy.has(reader(user))).length;
  if (path[0] === "notifications" && path[1] === "read") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const ids = Array.isArray(body.ids) ? body.ids : null;
    for (const notice of mine)
      if (!ids || ids.includes(notice.id)) notice.readBy.add(reader(user));
    return mockJson({ unread: unread() });
  }
  if (path[0] === "notifications") {
    const url = new URL(request.url);
    const limit = Number(url.searchParams.get("limit") ?? 20);
    const before = Number(url.searchParams.get("before") ?? 0);
    const onlyUnread = url.searchParams.get("unread") === "true";
    const rows = [...mine]
      .reverse()
      .filter((n) => !before || n.id < before)
      .filter((n) => !onlyUnread || !n.readBy.has(reader(user)));
    const page = rows.slice(0, limit);
    return mockJson({
      items: page.map(({ to: _to, readBy, ...n }) => {
        void _to;
        return { ...n, read: readBy.has(reader(user)) };
      }),
      unread: unread(),
      next: rows.length > limit ? page[page.length - 1].id : null,
    });
  }
  if (on && !user?.roles.includes("admin"))
    return mockError("没有权限：这项操作需要管理员", 403);
  if (path[0] === "notification-channels")
    return mockJson({
      email: { configured: false, from: null },
      web_url: "http://localhost:3000",
      webhooks: [...hooks.values()].map(hookOut),
    });
  // Webhooks
  if (path.length === 1) {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    if (!validUrl(body.url))
      return mockError("「地址」的格式、长度或范围不正确", 422);
    const hook: StoredHook = {
      webhook_id: `wh_${randomUUID().slice(0, 8)}`,
      name: String(body.name ?? "").trim(),
      kind: (KINDS.includes(body.kind as WebhookKind)
        ? body.kind
        : "generic") as WebhookKind,
      url: String(body.url).trim(),
      url_hint: "",
      events: EVENTS.filter((e) =>
        (body.events as NotificationEvent[]).includes(e),
      ),
      apps: Array.isArray(body.apps) ? (body.apps as string[]) : null,
      enabled: body.enabled !== false,
      created_at: new Date().toISOString(),
      last_sent_at: null,
      last_status: null,
      last_error: null,
    };
    hooks.set(hook.webhook_id, hook);
    return mockJson(hookOut(hook), 201);
  }
  const hook = hooks.get(path[1]);
  if (!hook) return mockError("没有找到这个 Webhook", 404);
  if (path[2] === "test") {
    const failed = hook.url.includes("fail");
    hook.last_sent_at = new Date().toISOString();
    hook.last_status = failed ? "error" : "ok";
    hook.last_error = failed
      ? "机器人返回错误 93000：invalid webhook url"
      : null;
    return mockJson(hookOut(hook));
  }
  if (request.method === "DELETE") {
    hooks.delete(hook.webhook_id);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (body.url !== undefined && !validUrl(body.url))
    return mockError("「地址」的格式、长度或范围不正确", 422);
  if (typeof body.name === "string") hook.name = body.name.trim();
  if (KINDS.includes(body.kind as WebhookKind))
    hook.kind = body.kind as WebhookKind;
  if (typeof body.url === "string") hook.url = body.url.trim();
  if (Array.isArray(body.events))
    hook.events = EVENTS.filter((e) =>
      (body.events as NotificationEvent[]).includes(e),
    );
  if ("apps" in body)
    hook.apps = Array.isArray(body.apps) ? (body.apps as string[]) : null;
  if (typeof body.enabled === "boolean") hook.enabled = body.enabled;
  return mockJson(hookOut(hook));
}
