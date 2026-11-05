import "server-only";
import { randomUUID } from "node:crypto";
import { ETL_V2_APP, etlV2Config } from "./mock-etl-v2";
import {
  knowledgeExists,
  knowledgeOptions,
  mockKnowledgeOptions,
} from "./mock-knowledge";
import type {
  AgentAppConfig,
  AppConfig,
  AppDetail,
  AppNode,
  AppVersion,
  CallableApp,
  Setting,
} from "../apps";
import type { SkillUsage } from "../skills";
import type { DatasourceUsage, UsageVia } from "../datasources";
import { isRecord } from "../runs";
import {
  APP_ID_PATTERN,
  agentAsFlow,
  isAgentConfig,
  isCodeConfig,
  isFlowConfig,
  type FlowConfig,
} from "../apps";
import {
  agentBlank,
  allSources,
  blankConfig,
  flowView,
  formConfig,
  helloConfig,
  missingReferences,
  mockModels,
  mockNodeTypes,
  mockTemplates,
  questionsConfig,
  simulateNode,
  validateFlow,
} from "./mock-flow-apps";
import { mockFlow } from "./mock-flow";
import { MockNodeFailure, lakehouseConfig } from "./mock-data-nodes";
import { allMockSources, mockToolService } from "./mock-datasources";
import { mockModelChoices } from "./mock-models";
import { buildMockManual } from "./mock-manual";
import { mockSkillOptions, mockSkillVersionExists } from "./mock-skills";
import { createMockEvalRun, mockRunsOf } from "./mock-runs";
import type { AppEntries, AppUsage, Entry, RunEntry } from "../app-entries";
import { ENTRIES, RUN_ENTRIES } from "../app-entries";
import {
  copyMockEvalSet,
  mockEvalSet,
  mockEvalSetOf,
  mockFieldSummary,
  mockHasEvalSet,
  mockImportEvalSet,
} from "./mock-evals";
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
  kind: "code" | "flow" | "agent";
  title: string;
  summary: string;
  created_at: string | null;
  archived?: boolean;
  secrets: Record<string, { updated_at: string }>;
  current: number;
  versions: AppVersion[];
  draft: AppVersion | null;
};
const models = mockModels;
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
          ? [
              ...modelSettings,
              ...(node.id === "plan"
                ? [
                    {
                      key: "knowledge_bases",
                      label: "参考知识库",
                      type: "knowledge_bases" as const,
                      help: "出方案前按需求检索这些知识库，方案里标出依据",
                    },
                    {
                      key: "knowledge_top_k",
                      label: "检索片段数",
                      type: "integer" as const,
                      min: 1,
                      max: 20,
                    },
                    {
                      key: "knowledge_mode",
                      label: "检索方式",
                      type: "enum" as const,
                      choices: [
                        { value: "hybrid", label: "混合" },
                        { value: "vector", label: "向量" },
                        { value: "keyword", label: "关键词" },
                      ],
                    },
                  ]
                : []),
            ]
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
  const config: AppConfig =
    app === "hello"
      ? helloConfig()
      : {
          nodes:
            app === "etl"
              ? {
                  profile: {
                    model: null,
                    skills: [],
                    params: { sample_rows: 5 },
                  },
                  plan: {
                    model: null,
                    skills: [{ name: "etl-plan", version: 1 }],
                    params: {
                      max_attempts: 3,
                      knowledge_bases: [],
                      knowledge_top_k: 5,
                      knowledge_mode: "hybrid",
                    },
                  },
                  codegen: {
                    model: null,
                    skills: [{ name: "etl-codegen", version: 1 }],
                    params: { max_attempts: 3 },
                  },
                  verify: {
                    model: null,
                    skills: [],
                    params: { max_dev_runs: 3 },
                  },
                }
              : { draft: { model: null, skills: [], params: {} } },
        };
  return {
    app,
    kind: app === "etl" ? "code" : "flow",
    title: app === "etl" ? "自然语言 ETL 工作流" : "问答示例",
    summary:
      app === "etl"
        ? "探查数据、给出方案，经人确认后生成 Databricks 管道代码。"
        : "用模型回答一个问题，请人确认；驳回时按意见重写。",
    created_at: app === "etl" ? null : "2026-10-28T02:00:00+00:00",
    secrets: {},
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
  [
    "retail-etl",
    {
      ...initial("hello"),
      app: "retail-etl",
      title: "零售数据 ETL",
      summary:
        "探查零售数据、确认业务口径，生成 Databricks 转换并预览 dev 结果。",
      versions: [
        { ...initial("hello").versions[0], config: lakehouseConfig(true) },
      ],
    },
  ],
  [
    "order-form",
    {
      ...initial("hello"),
      app: "order-form",
      title: "订单信息填写",
      summary: "人工填写订单号后返回结果。",
      versions: [{ ...initial("hello").versions[0], config: formConfig() }],
    },
  ],
  [
    ETL_V2_APP,
    {
      ...initial("hello"),
      app: ETL_V2_APP,
      title: "零售数据 ETL（第 2 版）",
      summary:
        "探查数据源、按口径知识库出方案，确认方案后抽取到 Databricks，在开发环境试算并算出答案，审核通过后交付。",
      versions: [{ ...initial("hello").versions[0], config: etlV2Config() }],
    },
  ],
  [
    "plan-questions",
    {
      ...initial("hello"),
      app: "plan-questions",
      title: "方案确认（带题）",
      summary: "出方案后请人回答问题并确认；改了答案或写了意见就重写方案。",
      versions: [
        { ...initial("hello").versions[0], config: questionsConfig() },
      ],
    },
  ],
]));
function current(entry: StoredApp) {
  return entry.versions.find((version) => version.version === entry.current)!;
}
function summary(entry: StoredApp) {
  return {
    app: entry.app,
    title: entry.title,
    summary: entry.summary,
    kind: entry.kind,
    created_at: entry.created_at,
    current_version: entry.current,
    has_draft: entry.draft !== null,
    has_eval_set: mockHasEvalSet(entry.app),
  };
}
function refresh(entry: StoredApp) {
  const evaluation = entry.draft?.eval;
  if (
    evaluation?.status !== "running" ||
    Date.now() - Date.parse(evaluation.started_at) < 6_000
  )
    return;
  const config = entry.draft?.config;
  const failed =
    config &&
    isCodeConfig(config) &&
    config.nodes.plan?.params.max_attempts === 1;
  evaluation.status = failed ? "failed" : "passed";
  evaluation.score = failed ? 0.6 : 0.9;
  evaluation.finished_at = new Date().toISOString();
  const set = entry.app === "etl" ? undefined : mockEvalSetOf(entry.app);
  evaluation.cases.forEach((item) => {
    item.status = "done";
    item.score = evaluation.score;
    const found = set?.cases.find((c) => c.id === item.id);
    if (found) {
      // Like the backend: "运行完成" and one line per check; every check passes here.
      item.score = 1;
      item.checks = [
        { kind: "completed", name: "运行完成", passed: true, detail: "" },
        ...found.expect.map((check) => ({
          kind: check.kind,
          name: `${check.path || "结果"} ${check.kind === "judge" ? "模型判断" : check.kind === "equals" ? "等于" : check.kind === "contains" ? "包含" : "不含"}`,
          passed: true,
          detail: "",
          path: check.path,
          ...(check.kind === "equals"
            ? { expected: check.value, actual: check.value }
            : {}),
        })),
      ];
    }
  });
  if (set) {
    evaluation.score = 1;
    evaluation.fields = mockFieldSummary(evaluation.cases);
  }
}
function startProject(config: FlowConfig) {
  const start = config.graph.nodes.find((node) => node.type === "start");
  return start?.type === "start" ? (start.project ?? null) : null;
}

// What an app's configuration runs as: an agent app's is its generated graph
// (docs/api/agent-apps.md, "生成的图").
function runnable(config: AppConfig): AppConfig {
  return isAgentConfig(config) ? agentAsFlow(config) : config;
}
const envOf = (config: AppConfig) =>
  isFlowConfig(config) || isAgentConfig(config) ? config.env.secrets : [];

function detail(entry: StoredApp): AppDetail {
  refresh(entry);
  const stored = current(entry).config;
  const config = runnable(stored);
  const flow = isFlowConfig(config)
    ? flowView(config, entry.title, entry.summary)
    : mockFlow(entry.app, [], true);
  return {
    ...summary(entry),
    pattern: flow.design!.pattern,
    knowledge: flow.design!.knowledge,
    skills_text: flow.design!.skills,
    nodes: isFlowConfig(config)
      ? flow.nodes.map((node) => ({
          ...node,
          design: node.design ?? null,
          settings: [],
        }))
      : nodesFor(entry.app),
    edges: flow.edges,
    current: current(entry),
    draft: entry.draft,
    inputs: isFlowConfig(config)
      ? (config.graph.nodes.find((node) => node.type === "start")?.inputs ?? [])
      : null,
    project: isFlowConfig(config) ? startProject(config) : null,
    chat: isAgentConfig(stored)
      ? stored.chat
      : isFlowConfig(stored) && stored.conversation?.enabled
        ? { opening: "", suggestions: [] }
        : null,
    secrets: [
      ...new Set([
        ...Object.keys(entry.secrets),
        ...envOf(config),
        ...(entry.draft ? envOf(entry.draft.config) : []),
      ]),
    ].map((name) => ({
      name,
      set: Boolean(entry.secrets[name]),
      updated_at: entry.secrets[name]?.updated_at ?? null,
    })),
    options: {
      models,
      default_model: "mock",
      model_choices: mockModelChoices(),
      skills: mockSkillOptions(),
      datasources: allSources().map(({ datasource_id, name, kind }) => ({
        datasource_id,
        name,
        kind,
      })),
      knowledge_bases: knowledgeOptions(),
      rerank_models: mockKnowledgeOptions.rerank_models,
      apps: callableApps(entry.app),
    },
  };
}
// The apps an agent may call (docs/api/agent-apps.md, "调用应用"), with why not when not.
function callableApps(self: string): CallableApp[] {
  return [...store.values()]
    .filter((other) => !other.archived && other.app !== self)
    .map((other) => {
      const config = runnable(current(other).config);
      const reason =
        other.kind === "code"
          ? "代码应用不能被调用"
          : !accessOf(other.app).apps.enabled
            ? "没有开放「被其他应用调用」"
            : isFlowConfig(config) &&
                config.graph.nodes.some((node) => node.type === "human")
              ? "有人工节点，不能被调用"
              : null;
      return {
        app: other.app,
        title: other.title,
        kind: other.kind,
        callable: reason === null,
        reason,
      };
    })
    .sort((a, b) => (a.app < b.app ? -1 : 1));
}

// Each app's entries (docs/api/app-access.md, "入口"): all open until switched off.
const globalEntries = globalThis as typeof globalThis & {
  aepMockEntries?: Map<string, Record<Entry, boolean>>;
};
const entrySwitches = (globalEntries.aepMockEntries ??= new Map());
function accessOf(app: string): AppEntries {
  const switches = entrySwitches.get(app);
  const runs = mockRunsOf(app);
  const last = (entry: Entry) =>
    runs.find((run) => runEntry(run) === entry)?.created_at ?? null;
  const state = (entry: Entry) => ({
    enabled: switches?.[entry] ?? true,
    last_used_at: last(entry),
  });
  return {
    use: state("use"),
    api: state("api"),
    mcp: state("mcp"),
    apps: { ...state("apps"), callers: callersOf(app) },
  };
}
export const mockEntryOpen = (app: string, entry: Entry) =>
  entrySwitches.get(app)?.[entry] ?? true;
function callersOf(app: string): AppEntries["apps"]["callers"] {
  return [...store.values()]
    .filter((other) => !other.archived)
    .flatMap((other) => {
      const config = runnable(current(other).config);
      if (!isFlowConfig(config)) return [];
      const vias = new Set<"node" | "agent">();
      for (const node of config.graph.nodes) {
        if (node.type === "app" && node.app === app) vias.add("node");
        if (
          node.type === "agent" &&
          node.apps?.some((item) => item.app === app && item.enabled !== false)
        )
          vias.add("agent");
      }
      return [...vias].map((via) => ({
        app: other.app,
        title: other.title,
        version: other.current,
        via,
      }));
    })
    .sort((a, b) =>
      a.app === b.app ? (a.via < b.via ? -1 : 1) : a.app < b.app ? -1 : 1,
    );
}
// Which entry a run came by, as the backend counts it (docs/api/ops.md).
function runEntry(run: {
  trigger?: { kind: string } | null;
  rerun_of?: string | null;
}): RunEntry {
  if (run.trigger?.kind === "schedule") return "schedule";
  if (run.trigger?.kind === "app") return "apps";
  return "use";
}
// An app's usage (docs/api/ops.md, "应用的使用情况"); draft trials and evaluations
// are not counted.
function usageOf(app: string, days: 7 | 30 | 90): AppUsage {
  const since = new Date(Date.now() - days * 86_400_000);
  const runs = mockRunsOf(app).filter(
    (run) => !run.draft && Date.parse(run.created_at ?? "") >= since.getTime(),
  );
  const done = runs.filter((run) => run.status === "done").length;
  const failed = runs.filter((run) => run.status === "error").length;
  const entries = Object.fromEntries(
    RUN_ENTRIES.map((key) => [key, 0]),
  ) as Record<RunEntry, number>;
  for (const run of runs) entries[runEntry(run)] += 1;
  const daily = new Map<string, number>();
  for (let i = days - 1; i >= 0; i--)
    daily.set(
      new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10),
      0,
    );
  for (const run of runs) {
    const day = (run.created_at ?? "").slice(0, 10);
    if (daily.has(day)) daily.set(day, daily.get(day)! + 1);
  }
  const talks = new Map<string, number>();
  for (const run of runs)
    if (run.conversation)
      talks.set(
        run.conversation.conversation_id,
        (talks.get(run.conversation.conversation_id) ?? 0) + 1,
      );
  const answered = runs.filter(
    (run) => run.status === "done" && run.conversation,
  );
  const ungrounded = answered.filter(
    (run) =>
      typeof run.result === "object" &&
      run.result !== null &&
      (run.result as { grounded?: unknown }).grounded === false,
  ).length;
  const agent = isAgentConfig(
    store.get(app) ? current(store.get(app)!).config : ({} as AppConfig),
  );
  // A mock turn makes one model call of made-up size.
  const cost = (n: number) => Math.round(n * 0.0004 * 10_000) / 10_000;
  return {
    days,
    since: since.toISOString(),
    totals: {
      runs: runs.length,
      done,
      error: failed,
      cancelled: runs.filter((run) => run.status === "cancelled").length,
      active: runs.filter(
        (run) => run.status === "running" || run.status === "waiting",
      ).length,
      delivery_seconds_median: runs.length ? 2 : null,
      wait_seconds_median: null,
      first_pass_rate: runs.length ? done / runs.length : null,
      model_cost_usd: cost(runs.length),
      model_calls: runs.length,
      unpriced_calls: 0,
      prompt_tokens: runs.length * 320,
      completion_tokens: runs.length * 100,
      compute_seconds: 0,
      compute_cost_usd: null,
    },
    daily: [...daily].map(([date, count]) => ({
      date,
      runs: count,
      done: count,
      error: 0,
      model_cost_usd: cost(count),
    })),
    entries,
    conversations: talks.size
      ? {
          count: talks.size,
          turns_avg:
            Math.round(
              ([...talks.values()].reduce((a, b) => a + b, 0) / talks.size) *
                10,
            ) / 10,
        }
      : agent
        ? { count: 0, turns_avg: 0 }
        : null,
    answers:
      agent || answered.length
        ? { answered: answered.length, ungrounded }
        : null,
    feedback: { good: 0, bad: 0 },
    tools: [],
    apps: [],
    truncated: false,
  };
}
export function mockSkillUsage(name: string): SkillUsage[] {
  return [...store.values()]
    .filter((entry) => !entry.archived)
    .flatMap((entry) => {
      const config = runnable(current(entry).config);
      const nodes = isFlowConfig(config)
        ? config.graph.nodes
            .filter((node) => "skills" in node)
            .map((node) => [node.id, node] as const)
        : isCodeConfig(config)
          ? Object.entries(config.nodes)
          : [];
      return nodes.flatMap(([node, value]) =>
        "skills" in value
          ? value.skills
              .filter((ref) => ref.name === name)
              .map((ref) => ({
                app: entry.app,
                node,
                version: entry.current,
                skill_version: ref.version,
              }))
          : [],
      );
    });
}
const WHOLE_REF = /^\s*\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}\s*$/;
const UUID =
  /^\{?([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})\}?$/i;
// UUIDs match whatever their case or braces; other ids as written.
function canonId(value: string) {
  const parts = value.trim().match(UUID);
  return parts ? parts.slice(1).join("-").toLowerCase() : value.trim();
}
// Like the backend (docs/api/datasources.md): the current version of each flow app that
// is not archived, once per app; a literal id wins over a start node's default.
export function mockDatasourceUsage(id: string): DatasourceUsage["apps"] {
  const wanted = canonId(id);
  return [...store.values()]
    .filter((entry) => !entry.archived)
    .flatMap((entry) => {
      const config = runnable(current(entry).config);
      if (!isFlowConfig(config)) return [];
      const start = config.graph.nodes.find((node) => node.type === "start");
      const inputs = start?.type === "start" ? start.inputs : [];
      const consts = new Map(
        config.env.vars.map((item) => [item.key, item.value]),
      );
      const found = new Map<string, UsageVia>();
      const add = (value: unknown, via: UsageVia) => {
        if (typeof value !== "string" || !value.trim()) return;
        const key = canonId(value);
        if (found.get(key) !== "literal") found.set(key, via);
      };
      for (const input of inputs)
        if (input.type === "datasource") add(input.default, "default_input");
      for (const node of config.graph.nodes) {
        const values =
          node.type === "tool" ||
          node.type === "etl_profile" ||
          node.type === "databricks_ingest" ||
          node.type === "data_check" ||
          node.type === "git_pr"
            ? [node.datasource]
            : node.type === "agent"
              ? node.tools.map((item) => item.datasource)
              : [];
        for (const value of values) {
          const ref = value.match(WHOLE_REF);
          if (ref?.[1] === "env") add(consts.get(ref[2]), "literal");
          else if (ref && ref[1] === start?.id)
            add(
              inputs.find((input) => input.key === ref[2])?.default,
              "default_input",
            );
          else if (!value.includes("{{")) add(value, "literal");
        }
      }
      const via = found.get(wanted);
      return via
        ? [{ app: entry.app, title: entry.title, version: entry.current, via }]
        : [];
    })
    .sort((a, b) => (a.app < b.app ? -1 : a.app > b.app ? 1 : 0));
}
// Like the backend, runs show their app's title as it is now, archived apps included.
export function mockAppTitle(app: string): string | undefined {
  return store.get(app)?.title;
}

// What a run of the app uses; an agent app's configuration is given as the graph it
// runs as, unless `raw` (a copy of the app).
export function mockAppSnapshot(
  app: string,
  version?: number,
  draft = false,
  raw = false,
) {
  const entry = store.get(app);
  if (!entry || entry.archived) return null;
  const snapshot = draft
    ? entry.draft
    : version === undefined
      ? current(entry)
      : entry.versions.find((item) => item.version === version);
  return snapshot
    ? {
        config: structuredClone(
          raw ? snapshot.config : runnable(snapshot.config),
        ),
        version: snapshot.version,
        title: entry.title,
        summary: entry.summary,
        secrets: Object.keys(entry.secrets),
      }
    : null;
}
// A few of the backend's checks of an agent app's configuration (docs/api/agent-apps.md).
function validateAgent(app: string, config: unknown): ValidationIssue[] {
  const at = (...loc: (string | number)[]) => ["body", "config", ...loc];
  if (!isRecord(config) || !isRecord(config.agent) || !isRecord(config.chat))
    return [{ loc: at(), msg: "智能体应用的配置要有 agent 和 chat" }];
  const issues: ValidationIssue[] = [];
  const { agent, chat } = config as unknown as AgentAppConfig;
  if (
    typeof agent.instructions !== "string" ||
    agent.instructions.length > 20000
  )
    issues.push({
      loc: at("agent", "instructions"),
      msg: "指令最多 20000 个字符",
    });
  if (agent.model !== null && !models.includes(agent.model))
    issues.push({ loc: at("agent", "model"), msg: "没有这个模型" });
  if (
    !Number.isInteger(agent.max_steps) ||
    agent.max_steps < 1 ||
    agent.max_steps > 20
  )
    issues.push({ loc: at("agent", "max_steps"), msg: "每轮最多 1–20 步" });
  if (
    !Number.isInteger(agent.history_turns) ||
    agent.history_turns < 0 ||
    agent.history_turns > 50
  )
    issues.push({ loc: at("agent", "history_turns"), msg: "历史轮数是 0–50" });
  if (agent.knowledge?.knowledge_bases.some((id) => !knowledgeExists(id)))
    issues.push({
      loc: at("agent", "knowledge", "knowledge_bases"),
      msg: "没有这个知识库，请重新选择",
    });
  const choices = callableApps(app);
  (agent.apps ?? []).forEach((item, index) => {
    const choice = choices.find((other) => other.app === item.app);
    const reason =
      item.app === app
        ? "不能调用自己"
        : !choice
          ? "没有这个应用"
          : choice.reason;
    if (reason)
      issues.push({ loc: at("agent", "apps", index, "app"), msg: reason });
    if (
      typeof item.when !== "string" ||
      !item.when.trim() ||
      item.when.length > 500
    )
      issues.push({
        loc: at("agent", "apps", index, "when"),
        msg: "写清什么时候调用，最多 500 个字符",
      });
  });
  if (typeof chat.opening !== "string" || chat.opening.length > 500)
    issues.push({ loc: at("chat", "opening"), msg: "开场白最多 500 个字符" });
  if (!Array.isArray(chat.suggestions) || chat.suggestions.length > 5)
    issues.push({ loc: at("chat", "suggestions"), msg: "推荐问题最多 5 个" });
  else
    chat.suggestions.forEach((question, index) => {
      if (
        typeof question !== "string" ||
        !question.trim() ||
        question.length > 100
      )
        issues.push({
          loc: at("chat", "suggestions", index),
          msg: "推荐问题 1–100 个字符",
        });
    });
  return issues;
}

function validateConfig(app: string, config: unknown): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (store.get(app)?.kind === "agent") return validateAgent(app, config);
  if (store.get(app)?.kind === "flow") {
    const errors = validateFlow(config);
    if (errors.length) return errors;
    for (const node of (config as FlowConfig).graph.nodes) {
      const knowledge =
        node.type === "knowledge"
          ? node
          : node.type === "agent"
            ? node.knowledge
            : null;
      // Like the backend: a data node set to a tool service is refused on save.
      const service =
        node.type === "etl_profile" || node.type === "databricks_ingest"
          ? mockToolService(node.datasource)
          : undefined;
      if (service)
        issues.push({
          loc: ["body", "config", "graph", "nodes", node.id, "datasource"],
          msg: `「${service.name}」是工具服务，这里要选数据库或上传文件的数据源`,
        });
      if (knowledge?.knowledge_bases.some((id) => !knowledgeExists(id)))
        issues.push({
          loc: [
            "body",
            "config",
            "graph",
            "nodes",
            node.id,
            ...(node.type === "agent" ? ["knowledge"] : []),
            "knowledge_bases",
          ],
          msg: "没有这个知识库，请重新选择",
        });
      // Like the backend (docs/api/git.md): code repositories only in 提交 PR nodes.
      const named =
        "datasource" in node && typeof node.datasource === "string"
          ? allMockSources().find(
              (item) => item.datasource_id === node.datasource,
            )
          : undefined;
      if (node.type === "git_pr") {
        const msg = !node.datasource
          ? "要选择代码仓库"
          : named && named.kind !== "git"
            ? `「${named.name}」不是代码仓库，这里要选代码仓库`
            : !named && !node.datasource.includes("{{")
              ? `没有代码仓库 ${node.datasource}`
              : null;
        if (msg)
          issues.push({
            loc: ["body", "config", "graph", "nodes", node.id, "datasource"],
            msg,
          });
        for (const [key, said] of [
          ["repo", "要填仓库"],
          ["files", "要引用要提交的文件，比如 {{check_code.bundle.files}}"],
          ["pr_title", "要填 PR 标题"],
        ] as const)
          if (!String(node[key] ?? "").trim())
            issues.push({
              loc: ["body", "config", "graph", "nodes", node.id, key],
              msg: said,
            });
      } else if (named?.kind === "git")
        issues.push({
          loc: ["body", "config", "graph", "nodes", node.id, "datasource"],
          msg: `「${named.name}」是代码仓库，只能用在提交 PR 节点里`,
        });
      // Like the backend (docs/api/flow-apps.md, "app 调用应用"), in short.
      if (node.type === "app") {
        const called = store.get(node.app);
        const msg = !node.app
          ? "要选择被调用的应用"
          : !called || called.archived
            ? `没有应用 ${node.app}`
            : called.kind !== "flow"
              ? `只能调用编排应用，「${called.title}」是代码应用`
              : null;
        if (msg)
          issues.push({
            loc: ["body", "config", "graph", "nodes", node.id, "app"],
            msg,
          });
      }
    }
    if (issues.length) return issues;
    return missingReferences(config as FlowConfig)
      .filter((problem) => problem.kind !== "secret")
      .map((problem) => ({
        loc: ["body", "config", "graph"],
        msg: problem.message,
      }));
  }
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
      const param =
        value.params[setting.key] ??
        (setting.type === "knowledge_bases"
          ? []
          : setting.key === "knowledge_top_k"
            ? 5
            : setting.key === "knowledge_mode"
              ? "hybrid"
              : undefined);
      let message = "";
      if (
        setting.type === "integer" &&
        (!Number.isInteger(param) ||
          typeof param !== "number" ||
          (setting.min !== undefined && param < setting.min) ||
          (setting.max !== undefined && param > setting.max))
      )
        message = `${setting.label}须为 ${setting.min ?? "不限"}~${setting.max ?? "不限"} 的整数`;
      if (
        setting.type === "knowledge_bases" &&
        (!Array.isArray(param) ||
          !param.every((id) =>
            knowledgeOptions().some((kb) => kb.kb_id === id),
          ))
      )
        message = "请选择存在的知识库";
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

function metadataIssues(
  body: Record<string, unknown>,
  creating = false,
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (
    creating &&
    (typeof body.app !== "string" || !APP_ID_PATTERN.test(body.app))
  )
    issues.push({
      loc: ["body", "app"],
      msg: "标识须以小写字母开头，使用小写字母、数字或连字符，共 2–64 字符",
    });
  if (
    (creating || body.title !== undefined) &&
    (typeof body.title !== "string" ||
      !body.title.trim() ||
      body.title.length > 50)
  )
    issues.push({ loc: ["body", "title"], msg: "名称为 1–50 个字符" });
  if (
    body.summary !== undefined &&
    (typeof body.summary !== "string" || body.summary.length > 500)
  )
    issues.push({ loc: ["body", "summary"], msg: "简介最多 500 个字符" });
  return issues;
}
function createEntry(
  body: Record<string, unknown>,
  config: AppConfig,
): StoredApp {
  const now = new Date().toISOString();
  return {
    app: body.app as string,
    kind: isAgentConfig(config) ? "agent" : "flow",
    title: (body.title as string).trim(),
    summary: typeof body.summary === "string" ? body.summary : "",
    created_at: now,
    current: 1,
    draft: null,
    secrets: {},
    versions: [
      {
        version: 1,
        status: "current",
        config: structuredClone(config),
        note: "初始版本",
        created_by: typeof body.actor === "string" ? body.actor : null,
        created_at: now,
        eval: null,
      },
    ],
  };
}
function draftVersion(config: AppConfig, actor: unknown): AppVersion {
  return {
    version: null,
    status: "draft",
    config: structuredClone(config),
    note: null,
    created_by: typeof actor === "string" ? actor : null,
    created_at: new Date().toISOString(),
    eval: null,
  };
}
function parseImport(
  body: Record<string, unknown>,
): Record<string, unknown> | Response {
  if (
    typeof body.yaml !== "string" ||
    Buffer.byteLength(body.yaml, "utf8") > 1024 * 1024
  )
    return invalid([
      { loc: ["body", "yaml"], msg: "请提供不超过 1 MB 的 YAML 文本" },
    ]);
  try {
    // JSON is a YAML subset; keep the mock dependency-free. Exports use this subset too.
    const file: unknown = JSON.parse(body.yaml);
    if (!isRecord(file) || file.format !== "aep-app/1") throw new Error();
    // A file without `kind` is a flow app's, as before agent apps.
    const issues =
      file.kind === "agent"
        ? validateAgent("", file.config)
        : validateFlow(file.config);
    if (issues.length) return invalid(issues);
    return file;
  } catch {
    return invalid([
      {
        loc: ["body", "yaml"],
        msg: "无法读取应用文件，请检查 aep-app/1 格式；本地模拟支持 JSON 格式的 YAML",
      },
    ]);
  }
}
export async function mockApps(
  request: Request,
  path: string[],
): Promise<Response> {
  if (path[0] === "node-types") return mockJson(mockNodeTypes);
  if (path[0] === "app-templates") return mockJson(mockTemplates);
  if (path.length === 1 && request.method === "GET")
    return mockJson(
      [...store.values()].filter((entry) => !entry.archived).map(summary),
    );
  if (path.length === 1 && request.method === "POST") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const issues = metadataIssues(body, true);
    if (issues.length) return invalid(issues);
    if (store.has(body.app as string)) return mockError("应用标识已存在", 409);
    if (!isRecord(body.from))
      return invalid([{ loc: ["body", "from"], msg: "请选择应用来源" }]);
    const from = body.from;
    let config: AppConfig | undefined;
    if (from.blank === true) config = blankConfig();
    else if (from.blank_agent === true) config = agentBlank();
    else if (typeof from.template === "string")
      config = mockTemplates.find((item) => item.id === from.template)?.config;
    else if (typeof from.app === "string") {
      const source = mockAppSnapshot(
        from.app,
        from.version as number | undefined,
        false,
        true,
      );
      if (source && isCodeConfig(source.config))
        return mockError("不能复制代码应用", 409);
      config = source?.config;
    }
    if (!config) return mockError("模板、应用或版本不存在", 404);
    const agent = isAgentConfig(config);
    // Like the backend: a template that needs a knowledge base or a data source starts
    // blank, with the template as the draft to finish.
    const needsSetup =
      typeof from.template === "string" &&
      mockTemplates
        .find((item) => item.id === from.template)
        ?.needs.some((need) =>
          agent
            ? need === "knowledge_base" || need === "datasource"
            : need === "knowledge_base",
        );
    const entry = createEntry(
      body,
      needsSetup ? (agent ? agentBlank() : blankConfig()) : config,
    );
    const template = mockTemplates.find((item) => item.id === from.template);
    // As the backend writes it; the asset catalog finds an app's template by it.
    if (template) entry.versions[0].note = `从模板「${template.title}」新建`;
    if (needsSetup) entry.draft = draftVersion(config, body.actor);
    store.set(entry.app, entry);
    if (typeof from.app === "string") copyMockEvalSet(from.app, entry.app);
    return mockJson(detail(entry), 201);
  }
  const isNewImport = path[1] === "import";
  const entry = isNewImport ? undefined : store.get(path[1]);
  if (!isNewImport && (!entry || entry.archived))
    return mockError("应用不存在", 404);
  if (entry && path[2] === "eval-set" && path[3] === "import")
    return mockImportEvalSet(request, entry.app, entry.kind);
  if (entry && path[2] === "eval-set")
    return mockEvalSet(request, entry.app, entry.kind, path[3] === "cases");
  if (isNewImport || (path[2] === "draft" && path[3] === "import")) {
    if (entry?.kind === "code") return mockError("代码应用不能导入", 409);
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const file = parseImport(body);
    if (file instanceof Response) return file;
    const config = file.config as AppConfig;
    if (entry && (entry.kind === "agent") !== isAgentConfig(config))
      return mockError(
        entry.kind === "agent"
          ? "这是编排应用的文件，不能导入智能体应用"
          : "这是智能体应用的文件，不能导入编排应用",
        409,
      );
    const problems = missingReferences(
      runnable(config) as FlowConfig,
      Object.keys(entry?.secrets ?? {}),
    );
    let target = entry;
    if (isNewImport) {
      const input = {
        ...body,
        title: body.title ?? file.title,
        summary: body.summary ?? file.summary,
      };
      const issues = metadataIssues(input, true);
      if (issues.length) return invalid(issues);
      if (store.has(body.app as string))
        return mockError("应用标识已存在", 409);
      target = createEntry(
        input,
        problems.some((problem) => problem.kind !== "secret")
          ? isAgentConfig(config)
            ? agentBlank()
            : blankConfig()
          : config,
      );
      if (problems.some((problem) => problem.kind !== "secret"))
        target.draft = draftVersion(config, body.actor);
      store.set(target.app, target);
    } else target!.draft = draftVersion(config, body.actor);
    return mockJson(
      { app: detail(target!), problems },
      isNewImport ? 201 : 200,
    );
  }
  if (!entry) return mockError("应用不存在", 404);
  refresh(entry);
  if (path[2] === "access") {
    if (request.method === "PUT") {
      const body = await readBody(request);
      if (body instanceof Response) return body;
      const issues: ValidationIssue[] = Object.entries(body)
        .filter(
          ([key, value]) =>
            !ENTRIES.includes(key as Entry) || typeof value !== "boolean",
        )
        .map(([key]) => ({
          loc: ["body", key],
          msg: "入口开关要是 true 或 false",
        }));
      if (issues.length) return invalid(issues);
      entrySwitches.set(entry.app, {
        ...(entrySwitches.get(entry.app) ?? {
          use: true,
          api: true,
          mcp: true,
          apps: true,
        }),
        ...(body as Partial<Record<Entry, boolean>>),
      });
    }
    return mockJson(accessOf(entry.app));
  }
  if (path[2] === "usage") {
    const days = Number(new URL(request.url).searchParams.get("days") ?? 30);
    if (![7, 30, 90].includes(days))
      return invalid([
        { loc: ["query", "days"], msg: "统计期只能是 7、30 或 90 天" },
      ]);
    return mockJson(usageOf(entry.app, days as 7 | 30 | 90));
  }
  if (path[2] === "convert-to-flow") {
    const source = current(entry).config;
    if (!isAgentConfig(source))
      return mockError("只有智能体应用能转成编排应用", 409);
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const input = {
      ...body,
      title: body.title ?? entry.title,
      summary: body.summary ?? entry.summary,
    };
    const issues = metadataIssues(input, true);
    if (issues.length) return invalid(issues);
    if (store.has(body.app as string)) return mockError("应用标识已存在", 409);
    const made = createEntry(input, agentAsFlow(source));
    made.versions[0].note = `从智能体应用「${entry.title}」v${entry.current} 转成`;
    store.set(made.app, made);
    copyMockEvalSet(entry.app, made.app);
    return mockJson(detail(made), 201);
  }
  if (path[2] === "manual") {
    const query = new URL(request.url).searchParams;
    const snapshot = mockAppSnapshot(
      entry.app,
      query.has("version") ? Number(query.get("version")) : undefined,
      query.get("draft") === "true",
    );
    return snapshot
      ? mockJson(
          buildMockManual(
            entry.app,
            snapshot.title,
            snapshot.summary,
            snapshot.version,
            snapshot.config,
          ),
        )
      : mockError("应用版本不存在", 404);
  }
  if (path.length === 2) {
    if (request.method === "GET") return mockJson(detail(entry));
    if (entry.kind === "code") return mockError("代码应用不能改名或归档", 409);
    if (request.method === "DELETE") {
      entry.archived = true;
      const { deleteMockSchedules } = await import("./mock-schedules");
      deleteMockSchedules(entry.app);
      return noContent();
    }
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const issues = metadataIssues(body);
    if (issues.length) return invalid(issues);
    if (typeof body.title === "string") entry.title = body.title.trim();
    if (typeof body.summary === "string") entry.summary = body.summary;
    return mockJson(detail(entry));
  }
  if (path[2] === "keys") return mockKeys(request, entry.app, path[3]);
  if (path[2] === "schedules") {
    const { mockAppSchedules } = await import("./mock-schedules");
    return mockAppSchedules(request, entry.app);
  }
  if (path[2] === "secrets") {
    if (request.method === "DELETE") {
      delete entry.secrets[path[3]];
      return noContent();
    }
    const body = await readBody(request);
    if (body instanceof Response) return body;
    if (
      typeof body.value !== "string" ||
      !body.value ||
      body.value.length > 4096
    )
      return invalid([
        { loc: ["body", "value"], msg: "密钥值为 1–4096 个字符" },
      ]);
    entry.secrets[path[3]] = { updated_at: new Date().toISOString() };
    return noContent();
  }
  if (path[2] === "nodes") {
    if (entry.kind === "code") return mockError("代码应用不能试跑单节点", 409);
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const config = (body.config ??
      entry.draft?.config ??
      current(entry).config) as FlowConfig;
    const node = config.graph?.nodes?.find((item) => item.id === path[3]);
    if (!node) return mockError("节点不存在", 404);
    if (
      [
        "databricks_ingest",
        "databricks_dev_run",
        "databricks_release",
        "data_check",
      ].includes(node.type)
    )
      return mockError("该类型不支持单节点试跑", 400);
    if (node.type === "app")
      return mockError("调用应用节点会真的发起一次运行，不能单独试跑", 422);
    if (node.type === "git_pr")
      return mockError("提交 PR 节点会真的在代码仓库开 PR，不能单独试跑", 422);
    if (["start", "human", "end"].includes(node.type))
      return invalid([
        {
          loc: ["body", "config", "graph", "nodes", node.id],
          msg: "该类型不支持单节点试跑",
        },
      ]);
    if (!isRecord(body.values))
      return invalid([{ loc: ["body", "values"], msg: "测试值必须是对象" }]);
    const pool: Record<string, Record<string, unknown>> = {
      env: Object.fromEntries(
        config.env.vars.map((item) => [item.key, item.value]),
      ),
      secret: Object.fromEntries(
        Object.keys(entry.secrets).map((name) => [name, "******"]),
      ),
    };
    for (const [key, value] of Object.entries(body.values)) {
      const [id, field] = key.split(".");
      if (id !== "env" && id !== "secret" && field)
        (pool[id] ??= {})[field] = value;
    }
    try {
      return mockJson({
        status: "done",
        ...simulateNode(node, pool),
        error: null,
        duration_ms: 20,
      });
    } catch (error) {
      return mockJson({
        status: "error",
        outputs: null,
        branch: null,
        error: {
          type:
            error instanceof MockNodeFailure
              ? error.type
              : node.type === "knowledge"
                ? "knowledge_error"
                : "model_error",
          message: error instanceof Error ? error.message : "节点出错",
        },
        rendered: {},
        duration_ms: 20,
      });
    }
  }
  if (path[2] === "versions" && request.method === "GET") {
    if (path.length === 3) return mockJson(entry.versions);
    if (entry.kind === "code") return mockError("代码应用不能导出", 409);
    const version = entry.versions.find(
      (item) => item.version === Number(path[3]),
    );
    if (!version) return mockError("应用版本不存在", 404);
    const config = runnable(version.config) as FlowConfig;
    const skills = config.graph.nodes.flatMap((node) =>
      "skills" in node ? node.skills : [],
    );
    const sourceIds = config.graph.nodes.flatMap((node) =>
      node.type === "tool"
        ? [node.datasource]
        : node.type === "agent"
          ? node.tools.map((item) => item.datasource)
          : [],
    );
    return new Response(
      JSON.stringify(
        {
          format: "aep-app/1",
          title: entry.title,
          summary: entry.summary,
          exported_from: { app: entry.app, version: version.version },
          ...(entry.kind === "agent" ? { kind: "agent" } : {}),
          config: version.config,
          requires: {
            skills,
            knowledge_bases: knowledgeOptions()
              .filter((kb) =>
                config.graph.nodes.some((node) =>
                  (node.type === "knowledge"
                    ? node.knowledge_bases
                    : node.type === "agent"
                      ? node.knowledge?.knowledge_bases
                      : []
                  )?.includes(kb.kb_id),
                ),
              )
              .map(({ kb_id, name }) => ({ kb_id, name })),
            datasources: allSources()
              .filter((source) => sourceIds.includes(source.datasource_id))
              .map(({ datasource_id, name, kind }) => ({
                datasource_id,
                name,
                kind,
              })),
            secrets: config.env.secrets,
          },
        },
        null,
        2,
      ),
      {
        headers: {
          "Content-Type": "text/yaml; charset=utf-8",
          "Content-Disposition": `attachment; filename="${entry.app}-v${version.version}.yaml"`,
          "Cache-Control": "no-store",
        },
      },
    );
  }
  if (path[2] === "draft" && request.method === "DELETE") {
    entry.draft = null;
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (request.method === "PUT") {
    const issues = validateConfig(entry.app, body.config);
    if (issues.length) return invalid(issues);
    entry.draft = draftVersion(body.config as AppConfig, body.actor);
    return mockJson(detail(entry));
  }
  if (path[2] === "versions") {
    if (body.actor !== undefined && typeof body.actor !== "string")
      return invalid([{ loc: ["body", "actor"], msg: "作者必须是文字" }]);
    const version = entry.versions.find(
      (item) => item.version === Number(path[3]),
    );
    if (!version) return mockError("应用版本不存在", 404);
    current(entry).status = "previous";
    version.status = "current";
    entry.current = version.version!;
    return mockJson(detail(entry));
  }
  if (!entry.draft) return mockError("请先保存应用草稿", 409);
  if (path[3] === "eval") {
    if (!mockHasEvalSet(entry.app)) return mockError("此应用没有评测集", 409);
    if (entry.draft.eval?.status === "running")
      return mockError("草稿已有评测在运行", 409);
    const config = entry.draft.config;
    const score =
      isCodeConfig(config) && config.nodes.plan.params.max_attempts === 1
        ? 0.6
        : 0.9;
    const set = entry.app === "etl" ? undefined : mockEvalSetOf(entry.app);
    const cases = set
      ? set.cases.map((item) => ({ id: item.id, title: item.title }))
      : ["orders-clean", "customers-clean"].map((id) => ({ id, title: null }));
    entry.draft.eval = {
      status: "running",
      score: null,
      threshold: set?.threshold ?? 0.8,
      case_count: cases.length,
      started_at: new Date().toISOString(),
      finished_at: null,
      error: null,
      cases: cases.map(({ id, title }) => ({
        id,
        title,
        status: "running",
        score: null,
        run_id: set ? null : createMockEvalRun(id, score),
        error: null,
        checks: [],
      })),
    };
    return mockJson(detail(entry), 202);
  }
  const issues = validatePublish(body);
  if (issues.length) return invalid(issues);
  if (mockHasEvalSet(entry.app) && entry.draft.eval?.status !== "passed")
    return mockError("草稿须先通过评测才能发布（阈值 0.8）", 409);
  if (
    isFlowConfig(entry.draft.config) &&
    missingReferences(entry.draft.config).some((item) => item.kind !== "secret")
  )
    return mockError("请先修复草稿缺失的引用", 409);
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

export function appsReferencingKnowledge(kb: string): string[] {
  return [...store.values()]
    .filter((entry) => {
      if (entry.archived) return false;
      const config = runnable(current(entry).config);
      return isFlowConfig(config)
        ? config.graph.nodes.some((node) =>
            (node.type === "knowledge"
              ? node.knowledge_bases
              : node.type === "agent"
                ? node.knowledge?.knowledge_bases
                : []
            )?.includes(kb),
          )
        : isCodeConfig(config) &&
            Object.values(config.nodes).some(
              (node) =>
                Array.isArray(node.params.knowledge_bases) &&
                node.params.knowledge_bases.includes(kb),
            );
    })
    .map((entry) => `${entry.title}（${entry.app}）`);
}

// API keys (docs/api/app-access.md): the secret is returned once, on creation.
type MockKey = {
  key_id: string;
  app: string;
  name: string;
  prefix: string;
  created_by: string | null;
  created_at: string;
  last_used_at: string | null;
};
const keyStore = globalThis as typeof globalThis & {
  aepMockKeys?: MockKey[];
};
const apiKeys = (keyStore.aepMockKeys ??= []);
async function mockKeys(request: Request, app: string, keyId?: string) {
  if (keyId) {
    const index = apiKeys.findIndex((k) => k.app === app && k.key_id === keyId);
    if (index < 0) return mockError("没有找到这个密钥", 404);
    apiKeys.splice(index, 1);
    return noContent();
  }
  if (request.method === "GET")
    return mockJson(apiKeys.filter((k) => k.app === app).reverse());
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const name = typeof body.name === "string" ? body.name.trim() : "";
  if (!name || name.length > 50)
    return mockError("「名称」的格式、长度或范围不正确", 422);
  if (apiKeys.filter((k) => k.app === app).length >= 20)
    return mockError("一个应用最多 20 个密钥", 409);
  const secret = `aep_${randomUUID().replaceAll("-", "")}${randomUUID().replaceAll("-", "").slice(0, 11)}`;
  const key: MockKey = {
    key_id: `key_${randomUUID().slice(0, 8)}`,
    app,
    name,
    prefix: secret.slice(0, 12),
    created_by: "演示用户",
    created_at: new Date().toISOString(),
    last_used_at: null,
  };
  apiKeys.push(key);
  return mockJson({ ...key, secret }, 201);
}

// The asset catalog (docs/api/catalog.md): each app that is not archived, with what its
// current version uses.
export type MockCatalogApp = {
  app: string;
  kind: "code" | "flow" | "agent";
  title: string;
  summary: string;
  version: number;
  updated_at: string;
  created_by: string | null;
  template: string | null;
  score: number | null;
  calls: { app: string; via: string }[];
};
export function mockCatalogApps(): MockCatalogApp[] {
  return [...store.values()]
    .filter((entry) => !entry.archived)
    .map((entry) => {
      const version = current(entry);
      const first = entry.versions.find((item) => item.version === 1);
      const config = runnable(version.config);
      return {
        app: entry.app,
        kind: entry.kind,
        title: entry.title,
        summary: entry.summary,
        version: entry.current,
        updated_at: version.created_at,
        created_by: first?.created_by ?? null,
        template: first?.note?.match(/^从模板「(.+)」新建$/)?.[1] ?? null,
        score: version.eval?.score ?? null,
        calls: isFlowConfig(config)
          ? config.graph.nodes.flatMap((node) =>
              node.type === "app" && node.app
                ? [{ app: node.app, via: node.title }]
                : [],
            )
          : [],
      };
    });
}
