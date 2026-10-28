// GET /runs/{id}/flow: how the run's app is built and the steps this run took.
// The contract is documented in backend/src/aep/api/runs.py.
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
};

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
