import "server-only";
import { ETL_V2_APP, etlV2Config } from "./mock-etl-v2";
import {
  knowledgeExists,
  knowledgeOptions,
  mockKnowledgeOptions,
} from "./mock-knowledge";
import type {
  AppConfig,
  AppDetail,
  AppNode,
  AppVersion,
  Setting,
} from "../apps";
import type { SkillUsage } from "../skills";
import type { DatasourceUsage, UsageVia } from "../datasources";
import { isRecord } from "../runs";
import { APP_ID_PATTERN, isFlowConfig, type FlowConfig } from "../apps";
import {
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
import { mockToolService } from "./mock-datasources";
import { buildMockManual } from "./mock-manual";
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
  kind: "code" | "flow";
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
  const config = entry.draft?.config;
  const failed =
    config &&
    !isFlowConfig(config) &&
    config.nodes.plan?.params.max_attempts === 1;
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
  const config = current(entry).config;
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
    secrets: [
      ...new Set([
        ...Object.keys(entry.secrets),
        ...(isFlowConfig(config) ? config.env.secrets : []),
        ...(entry.draft && isFlowConfig(entry.draft.config)
          ? entry.draft.config.env.secrets
          : []),
      ]),
    ].map((name) => ({
      name,
      set: Boolean(entry.secrets[name]),
      updated_at: entry.secrets[name]?.updated_at ?? null,
    })),
    options: {
      models,
      default_model: "mock",
      skills: mockSkillOptions(),
      datasources: allSources().map(({ datasource_id, name, kind }) => ({
        datasource_id,
        name,
        kind,
      })),
      knowledge_bases: knowledgeOptions(),
      rerank_models: mockKnowledgeOptions.rerank_models,
    },
  };
}
export function mockSkillUsage(name: string): SkillUsage[] {
  return [...store.values()]
    .filter((entry) => !entry.archived)
    .flatMap((entry) => {
      const config = current(entry).config;
      const nodes = isFlowConfig(config)
        ? config.graph.nodes
            .filter((node) => "skills" in node)
            .map((node) => [node.id, node] as const)
        : Object.entries(config.nodes);
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
      const config = current(entry).config;
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
          node.type === "databricks_ingest"
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

export function mockAppSnapshot(app: string, version?: number, draft = false) {
  const entry = store.get(app);
  if (!entry || entry.archived) return null;
  const snapshot = draft
    ? entry.draft
    : version === undefined
      ? current(entry)
      : entry.versions.find((item) => item.version === version);
  return snapshot
    ? {
        config: structuredClone(snapshot.config),
        version: snapshot.version,
        title: entry.title,
        summary: entry.summary,
        secrets: Object.keys(entry.secrets),
      }
    : null;
}
function validateConfig(app: string, config: unknown): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
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
    kind: "flow",
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
    const issues = validateFlow(file.config);
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
    else if (typeof from.template === "string")
      config = mockTemplates.find((item) => item.id === from.template)?.config;
    else if (typeof from.app === "string") {
      const source = mockAppSnapshot(
        from.app,
        from.version as number | undefined,
      );
      if (source && !isFlowConfig(source.config))
        return mockError("不能复制代码应用", 409);
      config = source?.config;
    }
    if (!config) return mockError("模板、应用或版本不存在", 404);
    const needsKnowledge =
      typeof from.template === "string" &&
      mockTemplates
        .find((item) => item.id === from.template)
        ?.needs.includes("knowledge_base");
    const entry = createEntry(body, needsKnowledge ? blankConfig() : config);
    if (needsKnowledge) entry.draft = draftVersion(config, body.actor);
    store.set(entry.app, entry);
    return mockJson(detail(entry), 201);
  }
  const isNewImport = path[1] === "import";
  const entry = isNewImport ? undefined : store.get(path[1]);
  if (!isNewImport && (!entry || entry.archived))
    return mockError("应用不存在", 404);
  if (isNewImport || (path[2] === "draft" && path[3] === "import")) {
    if (entry?.kind === "code") return mockError("代码应用不能导入", 409);
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const file = parseImport(body);
    if (file instanceof Response) return file;
    const config = file.config as FlowConfig;
    const problems = missingReferences(
      config,
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
          ? blankConfig()
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
    if (["databricks_ingest", "databricks_dev_run"].includes(node.type))
      return mockError("该类型不支持单节点试跑", 400);
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
    const config = version.config as FlowConfig;
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
          config,
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
    if (entry.app !== "etl") return mockError("此应用没有评测集", 409);
    if (entry.draft.eval?.status === "running")
      return mockError("草稿已有评测在运行", 409);
    const config = entry.draft.config;
    const score =
      !isFlowConfig(config) && config.nodes.plan.params.max_attempts === 1
        ? 0.6
        : 0.9;
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
      const config = current(entry).config;
      return isFlowConfig(config)
        ? config.graph.nodes.some((node) =>
            (node.type === "knowledge"
              ? node.knowledge_bases
              : node.type === "agent"
                ? node.knowledge?.knowledge_bases
                : []
            )?.includes(kb),
          )
        : Object.values(config.nodes).some(
            (node) =>
              Array.isArray(node.params.knowledge_bases) &&
              node.params.knowledge_bases.includes(kb),
          );
    })
    .map((entry) => `${entry.title}（${entry.app}）`);
}
