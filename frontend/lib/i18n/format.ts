// Times, durations and run wording in the reader's language. These replace the
// Chinese-only helpers in lib/runs.ts page by page; pass `t` and `locale` from useT()
// and useLocale().
import type { RunSummary, RunTrigger } from "../runs";
import { htmlLang, translate, type Locale } from "./locale";

type T = (text: string, vars?: Record<string, string | number>) => string;

export function formatDuration(t: T, seconds: number): string {
  if (seconds < 60) return t("{n} 秒", { n: Math.max(1, Math.round(seconds)) });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t("{n} 分钟", { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24)
    return minutes % 60
      ? t("{h} 小时 {m} 分钟", { h: hours, m: minutes % 60 })
      : t("{n} 小时", { n: hours });
  const days = Math.floor(hours / 24);
  return hours % 24
    ? t("{d} 天 {h} 小时", { d: days, h: hours % 24 })
    : t("{n} 天", { n: days });
}

export function formatTime(locale: Locale, iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString(htmlLang(locale), {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}

export function timeAgo(locale: Locale, iso: string, now: number): string {
  const t: T = (text, vars) => translate(locale, text, vars);
  const seconds = (now - Date.parse(iso)) / 1000;
  if (Number.isNaN(seconds)) return "";
  if (seconds < 60) return t("刚刚");
  if (seconds < 3_600) return t("{n} 分钟前", { n: Math.floor(seconds / 60) });
  if (seconds < 86_400)
    return t("{n} 小时前", { n: Math.floor(seconds / 3_600) });
  if (seconds < 7 * 86_400)
    return t("{n} 天前", { n: Math.floor(seconds / 86_400) });
  return formatTime(locale, iso);
}

// The machine's part and the wait for people, or only the whole time for runs from
// before waits were counted.
export function durationParts(t: T, run: RunSummary, now: number): string[] {
  if (!run.created_at) return [];
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  const total = Math.max(0, (end - Date.parse(run.created_at)) / 1000);
  if (Number.isNaN(total)) return [];
  if (typeof run.wait_seconds !== "number")
    return [t("用时 {d}", { d: formatDuration(t, total) })];
  const current = run.waiting_since
    ? Math.max(0, (end - Date.parse(run.waiting_since)) / 1000) || 0
    : 0;
  const waited = run.wait_seconds + current;
  const machine = t("机器 {d}", {
    d: formatDuration(t, Math.max(0, total - waited)),
  });
  return waited >= 1
    ? [machine, t("等人 {d}", { d: formatDuration(t, waited) })]
    : [machine];
}

// Where the run is: the step it is at, waits at, or failed in.
export function stepText(t: T, run: RunSummary): string {
  const step = run.step?.title;
  if (!step) return "";
  const text = {
    running: "进行到「{step}」",
    waiting: "停在「{step}」",
    error: "失败于「{step}」",
    cancelled: "取消于「{step}」",
    done: "",
  }[run.status];
  return text ? t(text, { step }) : "";
}

export const triggerText = (t: T, trigger: RunTrigger) =>
  trigger.kind === "app"
    ? t("由「{title}」调用", { title: trigger.title })
    : t("定时：{title}", { title: trigger.title });

export function runTitle(t: T, run: RunSummary): string {
  return run.title || t("{app} 的运行", { app: run.app_title || run.app });
}

export function versionLabel(t: T, run: RunSummary): string {
  return run.draft
    ? t("试运行（未发布版本）")
    : run.app_version
      ? `v${run.app_version}`
      : "";
}

export function formatNumber(locale: Locale, value: number): string {
  return new Intl.NumberFormat(htmlLang(locale)).format(value);
}

// Large counts in short form: 13万 / 130K.
export function formatCompact(locale: Locale, value: number): string {
  return new Intl.NumberFormat(htmlLang(locale), {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

// Month, day and time, for "last called" style notes.
export function formatShortTime(locale: Locale, iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString(htmlLang(locale), {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
