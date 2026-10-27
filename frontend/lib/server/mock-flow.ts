import "server-only";
import { randomUUID } from "node:crypto";
import type {
  AppDesign,
  Flow,
  FlowEdge,
  FlowNode,
  FlowStep,
  NodeDesign,
} from "../flow";

// A shortened copy of the designs in backend/src/aep/runtime/apps, for the local mock.
type Spec = Omit<NodeDesign, "inputs" | "outputs" | "model_step" | "tools"> &
  Partial<NodeDesign>;

function node(id: string, spec: Spec, model?: string): FlowNode {
  return {
    id,
    title: spec.title,
    kind: spec.kind,
    model: model ?? null,
    design: {
      inputs: "",
      outputs: "",
      tools: [],
      model_step: null,
      ...spec,
    },
  };
}

const START: FlowNode = { id: "__start__", title: "开始", kind: "terminal" };
const END: FlowNode = { id: "__end__", title: "结束", kind: "terminal" };

function edges(list: [string, string, boolean?][]): FlowEdge[] {
  return list.map(([source, target, conditional]) => ({
    source,
    target,
    conditional: Boolean(conditional),
  }));
}

const etl: Pick<Flow, "design" | "nodes" | "edges"> = {
  design: {
    title: "自然语言 ETL 工作流",
    summary: "探查数据、给出方案，经人确认后生成 Databricks 管道代码。",
    pattern: "固定流程的工作流（LangGraph 图），不是自主决定下一步的 agent。",
    knowledge: "没有接知识库。模型用到的知识是现场探查到的源表结构和样本。",
    skills: "没有使用 Skill。提示词写在代码里。",
  } satisfies AppDesign,
  nodes: [
    START,
    node("profile", {
      title: "探查数据",
      kind: "tool",
      summary: "通过数据源的只读连接器读取源表结构、统计和样本。",
      tools: ["数据源连接器（MCP，只读）：list_schemas、describe_table"],
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    node(
      "plan",
      {
        title: "出方案",
        kind: "model",
        summary: "模型根据需求和探查结果写出 ETL 方案。",
        model_step: "plan",
        prompt:
          "你是资深数据工程师，根据需求和源表写 ETL 方案（本地模拟提示词）。",
        knowledge: "探查结果，没有知识库",
        checks: "只能用探查到的表和列",
        retry: "最多 3 次",
      },
      "mock",
    ),
    node("review", {
      title: "确认方案",
      kind: "human",
      summary: "运行暂停，等人确认方案；驳回就回到“出方案”。",
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    node(
      "codegen",
      {
        title: "写代码",
        kind: "model",
        summary: "模型按确认的方案为每张目标表写转换代码。",
        model_step: "codegen",
        prompt: "你是资深 Databricks 数据工程师（本地模拟提示词）。",
        knowledge: "确认的方案和源表的列，没有知识库",
        checks: "静态检查",
        retry: "最多 3 次",
      },
      "mock",
    ),
    node("package", {
      title: "打包",
      kind: "code",
      summary: "生成 Databricks bundle 文件。",
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    node("verify", {
      title: "dev 试跑",
      kind: "tool",
      summary: "部署到 Databricks dev 目标并运行。",
      tools: ["Databricks REST API"],
      knowledge: null,
      prompt: null,
      checks: null,
      retry: "最多 3 次",
    }),
    node("publish", {
      title: "提交资产",
      kind: "tool",
      summary: "代码以 PR 提交到资产仓库。",
      tools: ["资产仓库（GitHub）"],
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    node("release", {
      title: "上线审批",
      kind: "human",
      summary: "等审批者决定是否上线到 prod。",
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    node("deploy_prod", {
      title: "上线 prod",
      kind: "tool",
      summary: "部署到 Databricks prod 目标并按计划运行。",
      tools: ["Databricks REST API"],
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    END,
  ],
  edges: edges([
    ["__start__", "profile"],
    ["profile", "plan"],
    ["plan", "review"],
    ["review", "plan", true],
    ["review", "codegen", true],
    ["codegen", "codegen", true],
    ["codegen", "package", true],
    ["package", "verify", true],
    ["package", "__end__", true],
    ["verify", "codegen", true],
    ["verify", "publish", true],
    ["verify", "__end__", true],
    ["publish", "release", true],
    ["publish", "__end__", true],
    ["release", "codegen", true],
    ["release", "deploy_prod", true],
    ["deploy_prod", "__end__"],
  ]),
};

const hello: Pick<Flow, "design" | "nodes" | "edges"> = {
  design: {
    title: "问答示例",
    summary: "用模型回答一个问题，请人确认；驳回时按意见重写。",
    pattern: "固定流程的工作流：回答、确认，驳回就回到回答。",
    knowledge: "没有接知识库，只用模型自身的知识。",
    skills: "没有使用 Skill。",
  },
  nodes: [
    START,
    node(
      "draft",
      {
        title: "回答",
        kind: "model",
        summary: "模型回答用户的问题。",
        prompt: "你是一个乐于助人的助手，用中文简洁地回答。",
        knowledge: null,
        checks: null,
        retry: null,
      },
      "mock",
    ),
    node("review", {
      title: "确认",
      kind: "human",
      summary: "运行暂停，等人确认回答。",
      knowledge: null,
      prompt: null,
      checks: null,
      retry: null,
    }),
    END,
  ],
  edges: edges([
    ["__start__", "draft"],
    ["draft", "review"],
    ["review", "draft", true],
    ["review", "__end__", true],
  ]),
};

export type MockSteps = FlowStep[];

export function addStep(
  steps: MockSteps,
  name: string,
  input: Record<string, unknown>,
  output: unknown,
  interrupt: unknown = null,
) {
  steps.push({
    id: randomUUID(),
    node: name,
    status: output === null ? "waiting" : "done",
    started_at: new Date().toISOString(),
    input,
    output,
    error: null,
    interrupt,
  });
}

// A person decided: the step that waited for them is done, with their decision.
export function decideStep(steps: MockSteps, decision: unknown) {
  const waiting = steps.findLast((step) => step.status === "waiting");
  if (!waiting) return;
  waiting.status = "done";
  waiting.output = { approvals: [decision] };
}

export function mockFlow(app: string, steps: MockSteps, done: boolean): Flow {
  const last = steps[steps.length - 1];
  return {
    ...(app === "etl" ? etl : hello),
    steps,
    current: done || !last || last.status === "done" ? null : last.node,
  };
}
