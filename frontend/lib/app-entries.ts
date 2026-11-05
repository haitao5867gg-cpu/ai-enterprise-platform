import type { Day, Metrics, Period } from "./ops";
import { requestJson } from "./runs";

// docs/api/app-access.md ("入口"): the ways into an app, each of which can be closed.
export const ENTRIES = ["use", "api", "mcp", "apps"] as const;
export type Entry = (typeof ENTRIES)[number];
export type EntryState = { enabled: boolean; last_used_at: string | null };
// The apps whose current version calls this one: by an app node, or as an agent's tool.
export type Caller = {
  app: string;
  title: string;
  version: number;
  via: "node" | "agent";
};
export type AppEntries = Record<Exclude<Entry, "apps">, EntryState> & {
  apps: EntryState & { callers: Caller[] };
};

export const entryLabels: Record<Entry, string> = {
  use: "使用页",
  api: "应用 API",
  mcp: "MCP",
  apps: "被其他应用调用",
};
export const entryHelp: Record<Entry, string> = {
  use: "平台里的使用者：使用页、新建运行页和对话。关掉后只有搭建者和管理员还能用来调试。",
  api: "外部系统用 API 密钥经 /api/v1 调用。关掉后密钥保留，重新打开就能用。",
  mcp: "Claude、Cursor 等 MCP 客户端用 API 密钥把它当工具。关掉后密钥保留。",
  apps: "编排应用的调用应用节点和智能体把它当工具调用。关掉后它们保存时报错，已发布的调用在运行时失败或不再提供。",
};

const accessPath = (app: string) => `/apps/${encodeURIComponent(app)}/access`;
export const getEntries = (app: string, signal?: AbortSignal) =>
  requestJson(accessPath(app), { signal }) as Promise<AppEntries>;
export const setEntries = (app: string, changes: Partial<Record<Entry, boolean>>) =>
  requestJson(accessPath(app), {
    method: "PUT",
    body: JSON.stringify(changes),
  }) as Promise<AppEntries>;

// docs/api/ops.md ("应用的使用情况").
export const RUN_ENTRIES = ["use", "api", "mcp", "apps", "schedule", "other"] as const;
export type RunEntry = (typeof RUN_ENTRIES)[number];
export const runEntryLabels: Record<RunEntry, string> = {
  ...entryLabels,
  schedule: "定时运行",
  other: "其他（评测、从某一步重跑等）",
};
export type AppUsage = {
  days: Period;
  since: string;
  totals: Metrics;
  daily: Day[];
  entries: Record<RunEntry, number>;
  conversations: { count: number; turns_avg: number } | null;
  answers: { answered: number; ungrounded: number } | null;
  feedback: { good: number; bad: number };
  tools: {
    tool: string;
    datasource: string | null;
    app: string | null;
    calls: number;
    errors: number;
  }[];
  apps: { app: string; title: string; calls: number; errors: number }[];
  truncated: boolean;
};
export const getUsage = (app: string, days: Period, signal?: AbortSignal) =>
  requestJson(`/apps/${encodeURIComponent(app)}/usage?days=${days}`, {
    signal,
  }) as Promise<AppUsage>;
