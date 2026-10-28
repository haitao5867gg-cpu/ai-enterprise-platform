import "server-only";
import type {
  AppConfig,
  AppDetail,
  AppNode,
  AppVersion,
  NodeConfig,
  Setting,
} from "../apps";
import type { SkillUsage } from "../skills";
import { isRecord } from "../runs";
import { mockFlow } from "./mock-flow";
import { mockSkillOptions, mockSkillVersionExists } from "./mock-skills";
import { createMockEvalRun } from "./mock-runs";
import {
  invalid,
  mockError,
  mockJson,
  noContent,
  readBody,
  validatePublish,
  type ValidationIssue,
} from "./mock-assets";

type StoredApp = {
  app: string;
  current: number;
  versions: AppVersion[];
  draft: AppVersion | null;
};
const models = [
  "mock",
  "gpt",
  "azure-gpt",
  "claude-bedrock",
  "qwen",
  "deepseek",
];
const modelSettings: Setting[] = [
  { key: "model", label: "模型", type: "model" },
  { key: "skills", label: "Skill", type: "skills" },
  {
    key: "max_attempts",
    label: "最多重写次数",
    type: "integer",
    min: 1,
    max: 5,
    help: "校验不通过时让模型重写的次数",
  },
];
function nodesFor(app: string): AppNode[] {
  return mockFlow(app, [], true).nodes.map((node) => ({
    ...node,
    design: node.design ?? null,
    settings:
      app === "etl"
        ? node.id === "plan" || node.id === "codegen"
          ? modelSettings
          : node.id === "profile"
            ? [
                {
                  key: "sample_rows",
                  label: "样本行数",
                  type: "integer",
                  min: 1,
                  max: 20,
                  help: "每张源表读取的样本行数",
                },
              ]
            : node.id === "verify"
              ? [
                  {
                    key: "max_dev_runs",
                    label: "最多 dev 试跑次数",
                    type: "integer",
                    min: 1,
                    max: 3,
                    help: "开发环境最多试跑次数",
                  },
                ]
              : []
        : node.id === "draft"
          ? [{ key: "model", label: "模型", type: "model" }]
          : [],
  }));
}
function initial(app: string): StoredApp {
  const config: AppConfig = {
    nodes:
      app === "etl"
        ? {
            profile: { model: null, skills: [], params: { sample_rows: 5 } },
            plan: {
              model: null,
              skills: [{ name: "etl-plan", version: 1 }],
              params: { max_attempts: 3 },
            },
            codegen: {
              model: null,
              skills: [{ name: "etl-codegen", version: 1 }],
              params: { max_attempts: 3 },
            },
            verify: { model: null, skills: [], params: { max_dev_runs: 3 } },
          }
        : { draft: { model: null, skills: [], params: {} } },
  };
  return {
    app,
    current: 1,
    draft: null,
    versions: [
      {
        version: 1,
        status: "current",
        config,
        note: "平台内置",
        created_by: null,
        created_at: "2026-10-28T02:00:00+00:00",
        eval: null,
      },
    ],
  };
}
const globalStore = globalThis as typeof globalThis & {
  aepMockApps?: Map<string, StoredApp>;
};
const store = (globalStore.aepMockApps ??= new Map([
  ["etl", initial("etl")],
  ["hello", initial("hello")],
]));
function current(entry: StoredApp) {
  return entry.versions.find((version) => version.version === entry.current)!;
}
function summary(entry: StoredApp) {
  const design = mockFlow(entry.app, [], true).design!;
  return {
    app: entry.app,
    title: design.title,
    summary: design.summary,
    current_version: entry.current,
    has_draft: entry.draft !== null,
    has_eval_set: entry.app === "etl",
  };
}
function refresh(entry: StoredApp) {
  const evaluation = entry.draft?.eval;
  if (
    evaluation?.status !== "running" ||
    Date.now() - Date.parse(evaluation.started_at) < 6_000
  )
    return;
  const failed = entry.draft?.config.nodes.plan?.params.max_attempts === 1;
  evaluation.status = failed ? "failed" : "passed";
  evaluation.score = failed ? 0.6 : 0.9;
  evaluation.finished_at = new Date().toISOString();
  evaluation.cases.forEach((item) => {
    item.status = "done";
    item.score = evaluation.score;
  });
}
function detail(entry: StoredApp): AppDetail {
  refresh(entry);
  const flow = mockFlow(entry.app, [], true);
  return {
    ...summary(entry),
    pattern: flow.design!.pattern,
    knowledge: flow.design!.knowledge,
    skills_text: flow.design!.skills,
    nodes: nodesFor(entry.app),
    edges: flow.edges,
    current: current(entry),
    draft: entry.draft,
    options: { models, default_model: "mock", skills: mockSkillOptions() },
  };
}
export function mockSkillUsage(name: string): SkillUsage[] {
  return [...store.values()].flatMap((entry) =>
    Object.entries(current(entry).config.nodes)
      .filter(([, node]) => node.skills.some((ref) => ref.name === name))
      .map(([node]) => ({ app: entry.app, node, version: entry.current })),
  );
}
function validateConfig(app: string, config: unknown): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (!isRecord(config) || !isRecord(config.nodes))
    return [{ loc: ["body", "config", "nodes"], msg: "节点配置必须是对象" }];
  const nodes = nodesFor(app).filter((node) => node.settings.length);
  for (const key of Object.keys(config.nodes))
    if (!nodes.some((node) => node.id === key))
      issues.push({
        loc: ["body", "config", "nodes", key],
        msg: "节点没有可调设置",
      });
  for (const node of nodes) {
    const value = config.nodes[node.id];
    const loc = ["body", "config", "nodes", node.id];
    if (
      !isRecord(value) ||
      !isRecord(value.params) ||
      !Array.isArray(value.skills)
    ) {
      issues.push({ loc, msg: "节点须包含 model、skills 列表和 params 对象" });
      continue;
    }
    if (
      value.model !== null &&
      (typeof value.model !== "string" || !models.includes(value.model))
    )
      issues.push({ loc: [...loc, "model"], msg: "请选择可用模型或平台默认" });
    if (
      !node.settings.some((setting) => setting.type === "model") &&
      value.model !== null
    )
      issues.push({ loc: [...loc, "model"], msg: "该节点不能设置模型" });
    const seen = new Set<string>();
    for (const ref of value.skills) {
      if (
        !isRecord(ref) ||
        typeof ref.name !== "string" ||
        !Number.isInteger(ref.version) ||
        !mockSkillVersionExists(ref.name, ref.version as number)
      )
        issues.push({
          loc: [...loc, "skills"],
          msg: "Skill 或已发布版本不存在",
        });
      else if (seen.has(ref.name))
        issues.push({ loc: [...loc, "skills"], msg: "Skill 不能重复引用" });
      else seen.add(ref.name);
    }
    if (
      !node.settings.some((setting) => setting.type === "skills") &&
      value.skills.length
    )
      issues.push({ loc: [...loc, "skills"], msg: "该节点不能引用 Skill" });
    for (const key of Object.keys(value.params))
      if (
        !node.settings.some(
          (setting) =>
            setting.key === key && !["model", "skills"].includes(setting.type),
        )
      )
        issues.push({ loc: [...loc, "params", key], msg: "不支持的设置项" });
    for (const setting of node.settings) {
      const param = value.params[setting.key];
      let message = "";
      if (
        setting.type === "integer" &&
        (!Number.isInteger(param) ||
          typeof param !== "number" ||
          (setting.min !== undefined && param < setting.min) ||
          (setting.max !== undefined && param > setting.max))
      )
        message = `${setting.label}须为 ${setting.min ?? "不限"}~${setting.max ?? "不限"} 的整数`;
      if (setting.type === "boolean" && typeof param !== "boolean")
        message = `${setting.label}须为开关值`;
      if (
        setting.type === "string" &&
        (typeof param !== "string" ||
          (setting.max_length !== undefined &&
            param.length > setting.max_length))
      )
        message = `${setting.label}文字长度不合法`;
      if (
        setting.type === "enum" &&
        !setting.choices.some((choice) => choice.value === param)
      )
        message = `${setting.label}须选择可用选项`;
      if (message)
        issues.push({ loc: [...loc, "params", setting.key], msg: message });
    }
  }
  return issues;
}

export async function mockApps(
  request: Request,
  path: string[],
): Promise<Response> {
  if (path.length === 1) return mockJson([...store.values()].map(summary));
  const entry = store.get(path[1]);
  if (!entry) return mockError("应用不存在", 404);
  refresh(entry);
  if (path.length === 2) return mockJson(detail(entry));
  if (path[2] === "versions" && request.method === "GET")
    return mockJson(entry.versions);
  if (path[2] === "draft" && request.method === "DELETE") {
    entry.draft = null;
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (request.method === "PUT") {
    const issues = validateConfig(entry.app, body.config);
    if (issues.length) return invalid(issues);
    entry.draft = {
      version: null,
      status: "draft",
      config: structuredClone(body.config) as {
        nodes: Record<string, NodeConfig>;
      },
      note: null,
      created_by: null,
      created_at: new Date().toISOString(),
      eval: null,
    };
    return mockJson(detail(entry));
  }
  if (path[2] === "versions") {
    if (body.actor !== undefined && typeof body.actor !== "string")
      return invalid([{ loc: ["body", "actor"], msg: "作者必须是文字" }]);
    const version = entry.versions.find(
      (item) => item.version === Number(path[3]),
    );
    if (!version) return mockError("应用版本不存在", 404);
    // Published snapshots (including their original author) stay immutable on activation.
    current(entry).status = "previous";
    version.status = "current";
    entry.current = version.version!;
    return mockJson(detail(entry));
  }
  if (!entry.draft) return mockError("请先保存应用草稿", 409);
  if (path[3] === "eval") {
    if (entry.app !== "etl") return mockError("此应用没有评测集", 409);
    if (entry.draft.eval?.status === "running")
      return mockError("草稿已有评测在运行", 409);
    const score =
      entry.draft.config.nodes.plan.params.max_attempts === 1 ? 0.6 : 0.9;
    entry.draft.eval = {
      status: "running",
      score: null,
      threshold: 0.8,
      started_at: new Date().toISOString(),
      finished_at: null,
      error: null,
      cases: ["orders-clean", "customers-clean"].map((id) => ({
        id,
        status: "running",
        score: null,
        run_id: createMockEvalRun(id, score),
        error: null,
      })),
    };
    return mockJson(detail(entry), 202);
  }
  const issues = validatePublish(body);
  if (issues.length) return invalid(issues);
  if (entry.app === "etl" && entry.draft.eval?.status !== "passed")
    return mockError("草稿须先通过评测才能发布（阈值 0.8）", 409);
  const version: AppVersion = {
    ...entry.draft,
    version: Math.max(...entry.versions.map((item) => item.version!)) + 1,
    status: "current",
    note: (body.note as string).trim(),
    created_by:
      typeof body.actor === "string" ? body.actor.trim() || null : null,
    created_at: new Date().toISOString(),
  };
  current(entry).status = "previous";
  entry.versions.unshift(version);
  entry.current = version.version!;
  entry.draft = null;
  return mockJson(detail(entry));
}
