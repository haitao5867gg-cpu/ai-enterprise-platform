import { requestJson } from "./runs";

// docs/api/notifications.md is the source of truth for these wire types.
export type NotificationEvent = "run.waiting" | "run.done" | "run.error";
export type WebhookKind = "generic" | "wecom" | "dingtalk" | "feishu";
export type AppNotification = {
  id: number;
  event: NotificationEvent;
  run_id: string;
  app: string;
  title: string;
  body: string;
  link: string;
  created_at: string;
  read: boolean;
};
export type NotificationPage = {
  items: AppNotification[];
  unread: number;
  next: number | null;
};
export type Webhook = {
  webhook_id: string;
  name: string;
  kind: WebhookKind;
  url_hint: string;
  events: NotificationEvent[];
  apps: string[] | null;
  enabled: boolean;
  created_at: string;
  last_sent_at: string | null;
  last_status: "ok" | "error" | null;
  last_error: string | null;
};
export type ChannelStatus = {
  email: { configured: boolean; from: string | null };
  web_url: string;
  webhooks: Webhook[];
};
export type WebhookInput = {
  name: string;
  kind: WebhookKind;
  url?: string;
  events: NotificationEvent[];
  apps: string[] | null;
  enabled: boolean;
};

export const EVENTS: NotificationEvent[] = [
  "run.waiting",
  "run.done",
  "run.error",
];
export const eventLabels: Record<NotificationEvent, string> = {
  "run.waiting": "待确认",
  "run.done": "完成",
  "run.error": "失败",
};
export const KINDS: WebhookKind[] = ["wecom", "dingtalk", "feishu", "generic"];
export const kindLabels: Record<WebhookKind, string> = {
  generic: "通用 JSON",
  wecom: "企业微信群机器人",
  dingtalk: "钉钉群机器人",
  feishu: "飞书群机器人",
};
export const kindHints: Record<WebhookKind, string> = {
  generic: "平台把通知以 JSON 推给这个地址，自己的系统接收后再转发。",
  wecom: "群设置里添加「群机器人」，复制它的 Webhook 地址。",
  dingtalk:
    "群设置里添加「自定义机器人」，安全设置选「自定义关键词」并填 AEP，复制 Webhook 地址。",
  feishu:
    "群设置里添加「自定义机器人」，安全设置选「自定义关键词」并填 AEP，复制 Webhook 地址。",
};

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export function listNotifications(
  params: { before?: number; unread?: boolean; limit?: number },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ limit: String(params.limit ?? 20) });
  if (params.before) query.set("before", String(params.before));
  if (params.unread) query.set("unread", "true");
  return requestJson(`/notifications?${query}`, {
    signal,
  }) as Promise<NotificationPage>;
}
export const markRead = (ids?: number[]) =>
  requestJson(
    "/notifications/read",
    json("POST", ids ? { ids } : {}),
  ) as Promise<{ unread: number }>;
export const getChannels = (signal?: AbortSignal) =>
  requestJson("/notification-channels", { signal }) as Promise<ChannelStatus>;
export const createWebhook = (input: WebhookInput) =>
  requestJson("/webhooks", json("POST", input)) as Promise<Webhook>;
export const updateWebhook = (id: string, changes: Partial<WebhookInput>) =>
  requestJson(
    `/webhooks/${encodeURIComponent(id)}`,
    json("PATCH", changes),
  ) as Promise<Webhook>;
export const deleteWebhook = (id: string) =>
  requestJson(`/webhooks/${encodeURIComponent(id)}`, json("DELETE"));
export const testWebhook = (id: string) =>
  requestJson(
    `/webhooks/${encodeURIComponent(id)}/test`,
    json("POST"),
  ) as Promise<Webhook>;
// The unread count changed here (read on the notification page): the bell refreshes.
export const UNREAD_EVENT = "aep-notifications-read";
