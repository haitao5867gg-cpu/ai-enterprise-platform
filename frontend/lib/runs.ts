import type { FormField } from "./apps";

export const runStatuses = ["running", "waiting", "done", "error"] as const;
export type RunStatus = (typeof runStatuses)[number];
export type Decision = string;
export type ValidationIssue = { loc: (string | number)[]; msg: string };
export type Message = { role: string; content: string };
export type Pending = {
  kind: "approval" | "form";
  prompt: string;
  options: Decision[];
  comment_required?: string[];
  option_labels?: Record<string, string>;
  fields?: FormField[];
  // Apps may add new detail types; the generic approval remains usable.
  details?: unknown;
};
export type Approval = Pending;
export type RunSummary = {
  app_version?: number | null;
  draft?: boolean;
  run_id: string;
  app: string;
  status: RunStatus;
  messages?: Message[];
};
export type Run = RunSummary & {
  messages: Message[];
  pending: Approval | null;
  result: unknown;
  error: string | null;
  trace_id?: string | null;
};

export const statusLabels: Record<RunStatus, string> = {
  running: "运行中",
  waiting: "等待确认",
  done: "已完成",
  error: "运行失败",
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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

function isSummary(
  value: unknown,
): value is RunSummary & Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    value.run_id.length > 0 &&
    typeof value.app === "string" &&
    isStatus(value.status) &&
    (value.messages === undefined || isMessages(value.messages))
  );
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
      path.endsWith("/test") ? 120_000 : 15_000,
    );
    response = await fetch(`/api${path}`, {
      ...init,
      cache: "no-store",
      signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
      headers: { "Content-Type": "application/json", ...init.headers },
    });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new ApiError("暂时无法连接服务，请检查连接后重试。");
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: path.startsWith("/runs")
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
    if (response.status === 409 || response.status === 422) {
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
    throw new ApiError(
      detail ? `${message} ${detail}` : message,
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
