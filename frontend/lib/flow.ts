// GET /runs/{id}/flow: how the run's app is built and the steps this run took.
// The contract is documented in backend/src/aep/api/runs.py.
import { translate } from "./i18n/locale";
import { ApiError, isRecord } from "./runs";

export type NodeKind = "tool" | "model" | "human" | "code" | "terminal";
export type StepStatus = "done" | "running" | "waiting" | "error" | "stopped";

export type AppDesign = {
  title: string;
  summary: string;
  pattern: string;
  knowledge: string;
  skills: string;
};

export type NodeDesign = {
  title: string;
  kind: NodeKind;
  summary: string;
  note?: string | null;
  inputs: string;
  outputs: string;
  tools: string[];
  model_step: string | null;
  prompt: string | null;
  knowledge: string | null;
  checks: string | null;
  retry: string | null;
};

export type FlowNode = {
  id: string;
  title: string;
  kind: NodeKind;
  type?: import("./apps").FlowNode["type"] | null;
  skills?: import("./apps").SkillRef[];
  model?: string | null;
  design?: NodeDesign | null;
};

export type FlowEdge = {
  source: string;
  target: string;
  conditional: boolean;
  label?: string | null;
};

export type FlowStep = {
  id: string;
  node: string;
  status: StepStatus;
  started_at: string | null;
  input: Record<string, unknown>;
  output: unknown;
  error: string | null;
  interrupt: unknown;
  // The developer view (docs/api/evals.md): when the step ended, how long it took and
  // the model calls made in it.
  finished_at?: string | null;
  duration_ms?: number | null;
  usage?: StepUsage | null;
};
export type StepUsage = {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number | null;
};

// The helpers below take `t` from useT() for the reader's language; without it they
// return Chinese.
type Tr = (text: string, vars?: Record<string, string | number>) => string;
const zh: Tr = (text, vars) => translate("zh", text, vars);
export function msText(ms: number | null | undefined, t: Tr = zh): string {
  if (ms == null) return "—";
  if (ms < 1000) return t("{ms} 毫秒", { ms });
  if (ms < 60_000) return t("{s} 秒", { s: (ms / 1000).toFixed(1) });
  const seconds = Math.round(ms / 1000);
  return t("{m} 分 {s} 秒", { m: Math.floor(seconds / 60), s: seconds % 60 });
}
export function tokensText(
  usage: StepUsage | null | undefined,
  t: Tr = zh,
): string {
  if (!usage) return t("没有模型调用");
  const total = usage.prompt_tokens + usage.completion_tokens;
  const cost = usage.cost_usd == null ? "" : ` · $${usage.cost_usd.toFixed(4)}`;
  return (
    t("{calls} 次调用 · {total} tokens（输入 {input} / 输出 {output}）", {
      calls: usage.calls,
      total: total.toLocaleString("en-US"),
      input: usage.prompt_tokens.toLocaleString("en-US"),
      output: usage.completion_tokens.toLocaleString("en-US"),
    }) + cost
  );
}

export type Flow = {
  design: AppDesign | null;
  nodes: FlowNode[];
  edges: FlowEdge[];
  steps: FlowStep[];
  current: string | null;
};

export const kindLabels: Record<NodeKind, string> = {
  tool: "工具调用",
  model: "模型生成",
  human: "人工确认",
  code: "平台程序",
  terminal: "",
};

export const stepLabels: Record<StepStatus, string> = {
  done: "已完成",
  running: "运行中",
  waiting: "等待确认",
  error: "失败",
  stopped: "已中断",
};

const kinds = ["tool", "model", "human", "code", "terminal"];
const statuses = ["done", "running", "waiting", "error", "stopped"];

function isNode(value: unknown): value is FlowNode {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    kinds.includes(value.kind as string) &&
    (value.design == null ||
      (isRecord(value.design) &&
        typeof value.design.summary === "string" &&
        Array.isArray(value.design.tools)))
  );
}

function isEdge(value: unknown): value is FlowEdge {
  return (
    isRecord(value) &&
    typeof value.source === "string" &&
    typeof value.target === "string" &&
    typeof value.conditional === "boolean"
  );
}

function isStep(value: unknown): value is FlowStep {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.node === "string" &&
    statuses.includes(value.status as string) &&
    isRecord(value.input)
  );
}

export function readFlow(value: unknown): Flow {
  if (
    !isRecord(value) ||
    !Array.isArray(value.nodes) ||
    !value.nodes.every(isNode) ||
    !Array.isArray(value.edges) ||
    !value.edges.every(isEdge) ||
    !Array.isArray(value.steps) ||
    !value.steps.every(isStep) ||
    !(value.current === null || typeof value.current === "string") ||
    !(value.design === null || isRecord(value.design))
  ) {
    throw new ApiError("流程数据格式不正确，请稍后重试。");
  }
  return value as Flow;
}
