import type { RunSource } from "./knowledge";
import type { FormField, Input } from "./apps";

export const runStatuses = [
  "running",
  "waiting",
  "done",
  "error",
  "cancelled",
] as const;
export type RunStatus = (typeof runStatuses)[number];
export type Decision = string;
export type ValidationIssue = { loc: (string | number)[]; msg: string };
// Who said what in the run, and at which step and when (null or missing in
// messages from before steps were named, and in the etl code app's).
export type Message = {
  role: string;
  content: string;
  node?: string | null;
  node_title?: string | null;
  actor?: string | null;
  at?: string | null;
};
export type Pending = {
  kind: "approval" | "form";
  prompt: string;
  options: Decision[];
  comment_required?: string[];
  option_labels?: Record<string, string>;
  fields?: FormField[];
  // The option a decision turns into when an answer or a remark changes it
  // (null when the step sets none).
  changed_branch?: string | null;
  // Apps may add new detail types; the generic approval remains usable.
  details?: unknown;
};
export type Approval = Pending;
export type RunStep = { node: string; title: string };
export type RunSummary = {
  app_version?: number | null;
  draft?: boolean;
  run_id: string;
  app: string;
  app_title?: string;
  status: RunStatus;
  // What the run is about, in one line; null when its input has no text.
  title?: string | null;
  created_by?: string | null;
  created_at?: string;
  updated_at?: string;
  finished_at?: string | null;
  step?: RunStep | null;
  // The current wait for a person, and the seconds of waits already over
  // (null: the run is from before waits were counted).
  waiting_since?: string | null;
  wait_seconds?: number | null;
  cancelled_by?: string | null;
  rerun_of?: string | null;
  // What a done flow run came to: the outcome of the end step it ended at
  // ("delivered", "stopped" or another word the app chose); null otherwise.
  outcome?: string | null;
};
export type RunFailure = {
  node: string | null;
  title: string | null;
  error_type: string | null;
  retryable: boolean;
};
export type RunActions = { cancel: boolean; retry: boolean; rerun: boolean };
export type Run = RunSummary & {
  input?: Record<string, unknown>;
  // What the configuration the run used asks for (a flow app's start inputs).
  inputs?: Input[] | null;
  failed?: RunFailure | null;
  actions?: RunActions;
  messages: Message[];
  pending: Approval | null;
  result: unknown;
  error: string | null;
  trace_id?: string | null;
  sources?: RunSource[];
};

export const statusLabels: Record<RunStatus, string> = {
  running: "运行中",
  waiting: "等待确认",
  done: "已完成",
  error: "运行失败",
  cancelled: "已取消",
};

const outcomeLabels: Record<string, string> = {
  delivered: "已交付",
  stopped: "已终止",
};

// The run's status in words: a done run says what it came to.
export function statusText(
  run: Pick<RunSummary, "status" | "outcome" | "step">,
): string {
  if (run.status !== "done" || !run.outcome) return statusLabels[run.status];
  return outcomeLabels[run.outcome] ?? run.step?.title ?? statusLabels.done;
}

// A run in one of these has ended and is not polled any more.
export const isFinished = (status: RunStatus) =>
  status === "done" || status === "error" || status === "cancelled";

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

export function failureText(failure: RunFailure): string {
  const where = failure.title ? `在「${failure.title}」这一步，` : "";
  const reason = failureReasons[failure.error_type ?? ""] ?? "运行出错了";
  return failure.retryable
    ? `${where}${reason}。这类问题多半是暂时的，可以从这一步重试。`
    : `${where}${reason}，重试多半还是一样。可以复制这次运行，把需求写得更具体后重新发起。`;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// A JSON object as a whole text, bare or in a code fence: what a model returns
// as data rather than prose.
export function jsonObject(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  const body =
    /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(trimmed)?.[1] ?? trimmed;
  if (!body.startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(body);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

function isStatus(value: unknown): value is RunStatus {
  return runStatuses.some((status) => status === value);
}

function isMessages(value: unknown): value is Message[] {
  return (
    Array.isArray(value) &&
    value.every(
      (message: unknown) =>
        isRecord(message) &&
        typeof message.role === "string" &&
        typeof message.content === "string",
    )
  );
}

const optional = (value: unknown, type: "string" | "number") =>
  value === undefined || value === null || typeof value === type;

function isSummary(
  value: unknown,
): value is RunSummary & Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    value.run_id.length > 0 &&
    typeof value.app === "string" &&
    isStatus(value.status) &&
    ["app_title", "title", "created_by", "created_at", "finished_at"].every(
      (key) => optional(value[key], "string"),
    ) &&
    optional(value.waiting_since, "string") &&
    optional(value.wait_seconds, "number") &&
    (value.step === undefined ||
      value.step === null ||
      (isRecord(value.step) && typeof value.step.title === "string"))
  );
}

export function runTitle(run: RunSummary): string {
  return run.title || `${run.app_title || run.app} 的运行`;
}

export function versionLabel(run: RunSummary): string {
  return run.draft
    ? "草稿试运行"
    : run.app_version
      ? `v${run.app_version}`
      : "";
}

// Where the run is, for its status: the step it is at, waits at, or failed in.
export function stepText(run: RunSummary): string {
  const title = run.step?.title;
  if (!title) return "";
  return run.status === "running"
    ? `进行到「${title}」`
    : run.status === "waiting"
      ? `停在「${title}」`
      : run.status === "error"
        ? `失败于「${title}」`
        : run.status === "cancelled"
          ? `取消于「${title}」`
          : "";
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24)
    return minutes % 60
      ? `${hours} 小时 ${minutes % 60} 分钟`
      : `${hours} 小时`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} 天 ${hours % 24} 小时` : `${days} 天`;
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}

// The time of day, for messages of a run that are mostly from the same day.
export function clockTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}

// A message's time; with its date when the day changed since the one before.
export function messageTime(iso: string, previous?: string | null): string {
  const date = new Date(iso);
  const before = previous ? new Date(previous) : null;
  if (
    Number.isNaN(date.getTime()) ||
    !before ||
    Number.isNaN(before.getTime()) ||
    before.toDateString() === date.toDateString()
  )
    return clockTime(iso);
  return `${date.getMonth() + 1}月${date.getDate()}日 ${clockTime(iso)}`;
}

export function timeAgo(iso: string, now: number): string {
  const seconds = (now - Date.parse(iso)) / 1000;
  if (Number.isNaN(seconds)) return "";
  if (seconds < 60) return "刚刚";
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)} 小时前`;
  if (seconds < 7 * 86_400) return `${Math.floor(seconds / 86_400)} 天前`;
  return formatTime(iso);
}

// How long the run took so far: the machine's part and the wait for people, or
// only the whole time for runs from before waits were counted.
export function durationText(run: RunSummary, now: number): string {
  if (!run.created_at) return "";
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  const total = Math.max(0, (end - Date.parse(run.created_at)) / 1000);
  if (Number.isNaN(total)) return "";
  if (typeof run.wait_seconds !== "number")
    return `用时 ${formatDuration(total)}`;
  const current = run.waiting_since
    ? Math.max(0, (end - Date.parse(run.waiting_since)) / 1000) || 0
    : 0;
  const waited = run.wait_seconds + current;
  const machine = `机器 ${formatDuration(Math.max(0, total - waited))}`;
  return waited >= 1 ? `${machine} · 等人 ${formatDuration(waited)}` : machine;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status = 0,
    public readonly issues: ValidationIssue[] = [],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function requestJson(
  path: string,
  init: RequestInit = {},
): Promise<unknown> {
  let response: Response;
  try {
    const timeout = AbortSignal.timeout(
      init.body instanceof FormData
        ? 300_000
        : path.endsWith("/test")
          ? 120_000
          : 15_000,
    );
    response = await fetch(`/api${path}`, {
      ...init,
      cache: "no-store",
      signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
      headers: {
        ...(init.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
        ...init.headers,
      },
    });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new ApiError("暂时无法连接服务，请检查连接后重试。");
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: /^\/runs\/[^/]+\/rerun$/.test(path)
        ? "原运行或它用的应用、版本已经不在了，请新建运行。"
        : path.startsWith("/runs")
          ? "没有找到这条运行记录，它可能已被移除。"
          : "没有找到请求的内容，它可能已被移除。",
      409: path.startsWith("/runs")
        ? "运行状态已发生变化，请刷新后查看。"
        : "内容状态已发生变化，请刷新后查看。",
      422: "提交内容未通过校验，请检查后重试。",
      503: "后端服务暂不可用，请稍后重试。",
      504: "服务响应超时，请稍后重试。",
    };
    let detail = "";
    let issues: ValidationIssue[] = [];
    if (
      response.status === 409 ||
      response.status === 422 ||
      response.status === 502
    ) {
      try {
        const value: unknown = await response.json();
        if (isRecord(value)) {
          if (typeof value.detail === "string") detail = value.detail;
          else if (Array.isArray(value.detail)) {
            issues = value.detail.filter(
              (item): item is ValidationIssue =>
                isRecord(item) &&
                Array.isArray(item.loc) &&
                typeof item.msg === "string",
            );
            detail = value.detail
              .filter(isRecord)
              .map(
                (item) =>
                  `${Array.isArray(item.loc) ? `${item.loc.join(".")}: ` : ""}${typeof item.msg === "string" ? item.msg : "校验失败"}`,
              )
              .join("；");
          }
        }
      } catch {
        // Keep the status-specific fallback for non-JSON upstream errors.
      }
    }
    const message = messages[response.status] ?? "请求未成功，请稍后重试。";
    // Creating or importing an app under a taken name: the detail says it all.
    const creating =
      init.method === "POST" && (path === "/apps" || path === "/apps/import");
    throw new ApiError(
      detail &&
      ((creating && response.status === 409) ||
        path.startsWith("/knowledge-bases") ||
        path.startsWith("/datasources"))
        ? detail
        : detail
          ? `${message} ${detail}`
          : message,
      response.status,
      issues,
    );
  }
  if (response.status === 204) return undefined;
  try {
    return await response.json();
  } catch {
    throw new ApiError("服务返回了无法读取的内容，请稍后重试。");
  }
}

export function readRunList(value: unknown): RunSummary[] {
  const runs = isRecord(value) ? value.runs : value;
  if (!Array.isArray(runs) || !runs.every(isSummary)) {
    throw new ApiError("运行列表格式不正确，请稍后重试。");
  }
  return runs;
}

export function readRun(value: unknown): Run {
  if (
    !isRecord(value) ||
    !isSummary(value) ||
    !isMessages(value.messages) ||
    !(value.error === null || typeof value.error === "string") ||
    !(
      value.trace_id === undefined ||
      value.trace_id === null ||
      typeof value.trace_id === "string"
    ) ||
    !("result" in value) ||
    !(
      value.pending === null ||
      (isRecord(value.pending) &&
        ["approval", "form"].includes(value.pending.kind as string) &&
        typeof value.pending.prompt === "string" &&
        Array.isArray(value.pending.options) &&
        value.pending.options.every(
          (option: unknown) => typeof option === "string",
        ))
    )
  ) {
    throw new ApiError("运行详情格式不正确，请稍后重试。");
  }
  return value as Run;
}

export function readReceipt(value: unknown): {
  run_id: string;
  status: RunStatus;
} {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    !value.run_id ||
    !isStatus(value.status)
  ) {
    throw new ApiError("服务未返回有效的运行编号，请返回列表核对运行记录。");
  }
  return { run_id: value.run_id, status: value.status };
}

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "发生了意外错误，请重试。";
}

export type CreateRunInput = {
  app: string;
  input: Record<string, unknown>;
  app_version?: number;
  draft?: boolean;
  requester?: string;
};
export type ResumeInput = {
  decision: string;
  comment?: string;
  values?: Record<string, unknown>;
  actor?: string;
};
export const createRun = async (input: CreateRunInput, signal?: AbortSignal) =>
  readReceipt(
    await requestJson("/runs", {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  );
export const cancelRun = async (
  id: string,
  input: { actor?: string; reason?: string },
  signal?: AbortSignal,
) =>
  readReceipt(
    await requestJson(`/runs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  );
export const retryRun = async (
  id: string,
  input: { actor?: string },
  signal?: AbortSignal,
) =>
  readReceipt(
    await requestJson(`/runs/${encodeURIComponent(id)}/retry`, {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  );
export const rerunRun = async (
  id: string,
  input: { input?: Record<string, unknown>; requester?: string },
  signal?: AbortSignal,
) =>
  readReceipt(
    await requestJson(`/runs/${encodeURIComponent(id)}/rerun`, {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  );
export const resumeRun = async (
  id: string,
  input: ResumeInput,
  signal?: AbortSignal,
) =>
  readReceipt(
    await requestJson(`/runs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),
  );
export function fieldErrors(
  error: unknown,
  section: "input" | "values",
): Record<string, string> {
  if (!(error instanceof ApiError)) return {};
  return Object.fromEntries(
    error.issues
      .filter(
        ({ loc }) =>
          loc.length === 3 &&
          loc[0] === "body" &&
          loc[1] === section &&
          typeof loc[2] === "string",
      )
      .map(({ loc, msg }) => [loc[2], msg]),
  );
}
