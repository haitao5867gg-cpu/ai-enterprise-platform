// Times, durations and run wording in the reader's language. These replace the
// Chinese-only helpers in lib/runs.ts page by page; pass `t` and `locale` from useT()
// and useLocale().
import {
  sampleLabels,
  type RunFailure,
  type RunPolicy,
  type RunProject,
  type RunSummary,
  type RunTrigger,
} from "../runs";
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

export const conversationText = (
  t: T,
  conversation: NonNullable<RunSummary["conversation"]>,
) =>
  t("对话「{title}」第 {turn} 轮", {
    title: conversation.title,
    turn: conversation.turn,
  });

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

// The time of day, for messages of a run that are mostly from the same day.
function clockTime(locale: Locale, date: Date): string {
  return date.toLocaleTimeString(htmlLang(locale), {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// A message's time; with its date when the day changed since the one before.
export function messageTime(
  locale: Locale,
  iso: string,
  previous?: string | null,
): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const before = previous ? new Date(previous) : null;
  if (
    !before ||
    Number.isNaN(before.getTime()) ||
    before.toDateString() === date.toDateString()
  )
    return clockTime(locale, date);
  const day = date.toLocaleDateString(htmlLang(locale), {
    month: "short",
    day: "numeric",
  });
  return `${day} ${clockTime(locale, date)}`;
}

// One line on what of the data goes to models, or null when the policy is the
// default one (everything, any model). Each part says whether the run's project or the
// platform set it.
export function policyText(
  t: T,
  policy: RunPolicy | undefined,
  project?: RunProject | null,
): string | null {
  if (!policy) return null;
  const parts: [string, string][] = [];
  const origin = (own: unknown) =>
    own != null ? t("项目设置") : t("平台设置");
  if (policy.samples !== "full")
    parts.push([
      t(sampleLabels[policy.samples]),
      origin(project?.policy.samples),
    ]);
  if (policy.models?.length)
    parts.push([
      t("只用模型 {models}", { models: policy.models.join(t("、")) }),
      origin(project?.policy.models),
    ]);
  if (!parts.length) return null;
  const join = (items: string[]) => items.join(t("；"));
  const of = (text: string, from: string) =>
    t("{text}（{from}）", { text, from });
  return parts.every(([, from]) => from === parts[0][1])
    ? of(join(parts.map(([text]) => text)), parts[0][1])
    : join(parts.map(([text, from]) => of(text, from)));
}

// What went wrong, for the people who asked for the run.
const failureReasons: Record<string, string> = {
  timeout: "等外部服务超时了",
  model_error: "模型服务出错了",
  tool_error: "调用工具时出错了",
  knowledge_error: "检索知识库时出错了",
  databricks_error: "Databricks 出错了",
  sandbox_unavailable: "代码沙箱暂时不可用",
  output_format: "模型几次给出的结果都不符合要求的格式",
  code_error: "代码节点里的程序报错了",
  step_limit: "智能体用完了允许的步数，还没有得出结果",
  max_visits: "流程在同一步来回的次数到了上限",
  max_steps: "流程走的总步数到了上限",
  input_error: "运行的输入或节点设置有问题",
};

export function failureText(t: T, failure: RunFailure): string {
  const reason = t(failureReasons[failure.error_type ?? ""] ?? "运行出错了");
  const text = failure.retryable
    ? t("{reason}。这类问题多半是暂时的，可以从这一步重试。", { reason })
    : t(
        "{reason}，重试多半还是一样。可以复制这次运行，把需求写得更具体后重新发起。",
        { reason },
      );
  return failure.title
    ? t("在「{step}」这一步，{text}", { step: failure.title, text })
    : text;
}
