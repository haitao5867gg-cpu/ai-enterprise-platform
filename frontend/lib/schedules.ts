import type { ProjectChoice } from "./projects";
import { requestJson } from "./runs";

// docs/api/schedules.md is the source of truth for these wire types.
export type Schedule = {
  schedule_id: string;
  app: string;
  title: string;
  cron: string;
  timezone: string;
  input: Record<string, unknown>;
  project: ProjectChoice | null;
  enabled: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  next_run_at: string | null;
  next_runs: string[];
  last_run: { run_id: string; at: string; status: string | null } | null;
  last_error: string | null;
};
export type ScheduleBody = {
  title: string;
  cron: string;
  timezone?: string;
  input: Record<string, unknown>;
  project: ProjectChoice | null;
  enabled?: boolean;
  requester?: string;
};
export type SchedulePreview = {
  cron: string;
  timezone: string;
  next_runs: string[];
};

const id = (scheduleId: string) =>
  `/schedules/${encodeURIComponent(scheduleId)}`;
export const listSchedules = (app: string, signal?: AbortSignal) =>
  requestJson(`/apps/${encodeURIComponent(app)}/schedules`, {
    signal,
  }) as Promise<Schedule[]>;
export const createSchedule = (app: string, body: ScheduleBody) =>
  requestJson(`/apps/${encodeURIComponent(app)}/schedules`, {
    method: "POST",
    body: JSON.stringify(body),
  }) as Promise<Schedule>;
export const updateSchedule = (
  scheduleId: string,
  body: Partial<ScheduleBody>,
) =>
  requestJson(id(scheduleId), {
    method: "PATCH",
    body: JSON.stringify(body),
  }) as Promise<Schedule>;
export const deleteSchedule = (scheduleId: string) =>
  requestJson(id(scheduleId), { method: "DELETE" });
export const runSchedule = (scheduleId: string) =>
  requestJson(`${id(scheduleId)}/run`, { method: "POST" }) as Promise<{
    run_id: string;
    status: string;
  }>;
export const previewSchedule = (
  cron: string,
  timezone: string,
  signal?: AbortSignal,
) =>
  requestJson(`/schedules/preview?${new URLSearchParams({ cron, timezone })}`, {
    signal,
  }) as Promise<SchedulePreview>;

// The time zones offered; one a schedule already has is offered too.
export const TIMEZONES = [
  { value: "Asia/Shanghai", label: "北京时间（Asia/Shanghai）" },
  { value: "UTC", label: "UTC" },
  { value: "Asia/Singapore", label: "新加坡（Asia/Singapore）" },
  { value: "Europe/London", label: "伦敦（Europe/London）" },
  { value: "America/New_York", label: "纽约（America/New_York）" },
];

// The common kinds of schedule picked on the page, and "write the cron yourself".
export type PresetKind =
  | "daily"
  | "weekdays"
  | "weekly"
  | "monthly"
  | "hourly"
  | "custom";
export type Preset = {
  kind: PresetKind;
  hour: number;
  minute: number;
  // 0 is Sunday, as in cron.
  weekday: number;
  day: number;
  cron: string;
};
export const PRESET_LABELS: Record<PresetKind, string> = {
  daily: "每天",
  weekdays: "周一到周五",
  weekly: "每周",
  monthly: "每月",
  hourly: "每小时",
  custom: "自己写 cron",
};
export const WEEKDAYS = [
  "周日",
  "周一",
  "周二",
  "周三",
  "周四",
  "周五",
  "周六",
];

export const DEFAULT_PRESET: Preset = {
  kind: "daily",
  hour: 8,
  minute: 0,
  weekday: 1,
  day: 1,
  cron: "0 8 * * *",
};

export function presetCron(preset: Preset): string {
  const { minute: m, hour: h } = preset;
  switch (preset.kind) {
    case "daily":
      return `${m} ${h} * * *`;
    case "weekdays":
      return `${m} ${h} * * 1-5`;
    case "weekly":
      return `${m} ${h} * * ${preset.weekday}`;
    case "monthly":
      return `${m} ${h} ${preset.day} * *`;
    case "hourly":
      return `${m} * * * *`;
    default:
      return preset.cron.trim().split(/\s+/).join(" ");
  }
}

const NUMBER = /^\d{1,2}$/;
// The preset a saved cron is, or "custom" with the cron as it is.
export function readPreset(cron: string): Preset {
  const parts = cron.trim().split(/\s+/);
  const base = { ...DEFAULT_PRESET, kind: "custom" as const, cron };
  if (parts.length !== 5) return base;
  const [m, h, dom, mon, dow] = parts;
  if (!NUMBER.test(m) || +m > 59 || mon !== "*") return base;
  const minute = +m;
  if (h === "*" && dom === "*" && dow === "*")
    return { ...base, kind: "hourly", minute };
  if (!NUMBER.test(h) || +h > 23) return base;
  const at = { ...base, minute, hour: +h };
  if (dom === "*" && dow === "*") return { ...at, kind: "daily" };
  if (dom === "*" && dow === "1-5") return { ...at, kind: "weekdays" };
  if (dom === "*" && /^[0-7]$/.test(dow))
    return { ...at, kind: "weekly", weekday: +dow % 7 };
  if (NUMBER.test(dom) && +dom >= 1 && +dom <= 31 && dow === "*")
    return { ...at, kind: "monthly", day: +dom };
  return base;
}

const pad = (n: number) => String(n).padStart(2, "0");
// What a cron means in words, e.g. "周一到周五 08:00"; the cron itself when it is none
// of the common kinds.
export function cronText(cron: string): string {
  const p = readPreset(cron);
  const at = `${pad(p.hour)}:${pad(p.minute)}`;
  switch (p.kind) {
    case "daily":
      return `每天 ${at}`;
    case "weekdays":
      return `周一到周五 ${at}`;
    case "weekly":
      return `每${WEEKDAYS[p.weekday]} ${at}`;
    case "monthly":
      return `每月 ${p.day} 日 ${at}`;
    case "hourly":
      return p.minute ? `每小时第 ${p.minute} 分` : "每小时整点";
    default:
      return `cron：${cron}`;
  }
}

// A time shown in the schedule's own time zone, e.g. "2026-11-09 周一 08:00".
export function zonedTime(value: string, timezone: string): string {
  try {
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("zh-CN", {
        timeZone: timezone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(new Date(value))
        .map((part) => [part.type, part.value]),
    );
    return `${parts.year}-${parts.month}-${parts.day} ${parts.weekday} ${parts.hour}:${parts.minute}`;
  } catch {
    return value;
  }
}
