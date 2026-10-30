import type {
  KnowledgeOption,
  Reference,
  RerankModel,
  Retrieval,
} from "./knowledge";
import type { FlowEdge, NodeDesign, NodeKind } from "./flow";
import { ApiError, isRecord, requestJson } from "./runs";

// docs/api/apps-and-skills.md — names and payloads follow the backend contract.
export type SkillRef = { name: string; version: number };
export type NodeConfig = {
  model: string | null;
  skills: SkillRef[];
  params: Record<string, string | number | boolean | string[]>;
};
export type CodeConfig = { nodes: Record<string, NodeConfig> };
export type InputType =
  | "text"
  | "textarea"
  | "number"
  | "boolean"
  | "select"
  | "datasource";
export type Input<T extends InputType = InputType> = {
  key: string;
  label: string;
  type: T;
  required?: boolean;
  default?: string | number | boolean | null;
  choices?: { value: string; label: string }[];
  max_length?: number;
};
export type FormField = Input<Exclude<InputType, "datasource">>;
export type ValueType = "string" | "number" | "boolean" | "array" | "object";
export type OutputField = {
  key: string;
  type: ValueType;
  description?: string;
};
export type EnvVar = {
  key: string;
  type: "string" | "number" | "boolean";
  value: string | number | boolean;
  description?: string;
};
export type OnError = { retry?: { max: number; interval_seconds: number } } & (
  | { then: "stop" | "branch" }
  | { then: "default"; default: Record<string, unknown> }
);
export type NodeBase = {
  id: string;
  title: string;
  note?: string;
  max_visits?: number;
  ui?: { x: number; y: number };
};
type ModelSettings = { model: string | null; skills: SkillRef[] };
type Fallible = { on_error?: OnError };
export type StartNode = NodeBase & { type: "start"; inputs: Input[] };
export type LlmNode = NodeBase &
  ModelSettings &
  Fallible & {
    type: "llm";
    system?: string;
    prompt: string;
    output: { kind: "text" } | { kind: "json"; fields: OutputField[] };
    // A text answer that is only for later steps stays out of the run process.
    quiet?: boolean;
  };
export type ClassifierNode = NodeBase &
  ModelSettings &
  Fallible & {
    type: "classifier";
    text: string;
    instruction?: string;
    classes: { key: string; label: string; description?: string }[];
  };
export type Condition = {
  left: unknown;
  op:
    | "eq"
    | "ne"
    | "contains"
    | "not_contains"
    | "gt"
    | "gte"
    | "lt"
    | "lte"
    | "empty"
    | "not_empty";
  right?: unknown;
};
export type ConditionNode = NodeBase & {
  type: "condition";
  rules: {
    key: string;
    label: string;
    match: "all" | "any";
    conditions: Condition[];
  }[];
};
export type ToolNode = NodeBase &
  Fallible & {
    type: "tool";
    datasource: string;
    tool: string;
    args: Record<string, unknown>;
  };
export type KnowledgeNode = NodeBase &
  Fallible & {
    type: "knowledge";
    knowledge_bases: string[];
    query: string;
    top_k: number;
    min_score: number;
    retrieval?: Retrieval;
  };
export type AgentKnowledge = Pick<
  KnowledgeNode,
  "knowledge_bases" | "top_k" | "min_score" | "retrieval"
>;
export type AgentStep = {
  tool: string;
  datasource: string | null;
  args: Record<string, unknown>;
  error: string | null;
  hits?: number;
};
export type AgentOutput = {
  text: string;
  steps: AgentStep[];
  hits: Reference[];
};
export type AgentNode = NodeBase &
  ModelSettings &
  Fallible & {
    type: "agent";
    task: string;
    tools: { datasource: string; tools: string[] | null }[];
    knowledge?: AgentKnowledge | null;
    max_steps: number;
  };
// The extended settings (view, data, the approval's fields, changed_branch and
// options[].requires) are written in templates or imported files; the editor shows them
// and keeps them as they are (docs/api/flow-apps.md, "人工节点的扩展设置").
export type HumanNode = NodeBase & {
  type: "human";
  prompt: string;
  show?: string;
  view?: string | null;
  data?: Record<string, unknown> | null;
} & (
    | {
        mode: "approval";
        options: { key: string; label: string; requires?: unknown }[];
        comment_required?: string[];
        // The questions: a list, or one reference such as {{check_plan.questions}}.
        fields?: unknown;
        changed_branch?: string | null;
      }
    | { mode: "form"; fields: FormField[] }
  );
export type TemplateNode = NodeBase & { type: "template"; template: string };
export type CodeNode = NodeBase &
  Fallible & {
    type: "code";
    code: string;
    inputs: Record<string, unknown>;
    outputs: OutputField[];
    timeout_seconds: number;
  };
export type EndNode = NodeBase & {
  type: "end";
  outputs: { key: string; from: string[] }[];
};
export type EtlProfileNode = NodeBase &
  Fallible & {
    type: "etl_profile";
    datasource: string;
    tables: string | string[];
    sample_rows: number;
  };
export type EtlPlanCheckNode = NodeBase &
  Fallible & {
    type: "etl_plan_check";
    reply: string;
    profile: string;
    hits: string[];
    language?: string;
    max_attempts: number;
  };
export type DatabricksIngestNode = NodeBase &
  Fallible & {
    type: "databricks_ingest";
    datasource: string;
    tables: string | string[];
    max_rows: number;
  };
export type EtlCodeCheckNode = NodeBase &
  Fallible & {
    type: "etl_code_check";
    reply: string;
    plan: string;
    source_name: string;
    name?: string;
    max_attempts: number;
  };
export type DatabricksDevRunNode = NodeBase &
  Fallible & {
    type: "databricks_dev_run";
    bundle: string;
    max_runs: number;
    preview_rows: number;
  };
export type FlowNode =
  | StartNode
  | LlmNode
  | ClassifierNode
  | ConditionNode
  | ToolNode
  | KnowledgeNode
  | AgentNode
  | HumanNode
  | TemplateNode
  | CodeNode
  | EtlProfileNode
  | EtlPlanCheckNode
  | DatabricksIngestNode
  | EtlCodeCheckNode
  | DatabricksDevRunNode
  | EndNode;
export type Edge = { from: string; to: string; branch?: string | null };
export type FlowConfig = {
  schema_version: 1;
  env: { vars: EnvVar[]; secrets: string[] };
  graph: { nodes: FlowNode[]; edges: Edge[] };
};
export type AppConfig = CodeConfig | FlowConfig;
export function isFlowConfig(config: AppConfig): config is FlowConfig {
  return "graph" in config;
}
type WithoutBase<T> = T extends unknown
  ? Omit<T, keyof NodeBase | "type" | "on_error">
  : never;
type SettingsFor<K extends FlowNode["type"]> = WithoutBase<
  Extract<FlowNode, { type: K }>
>;
export type NodeType = {
  [K in FlowNode["type"]]: {
    type: K;
    label: string;
    description: string;
    category: "model" | "logic" | "tool" | "data" | "human" | "terminal";
    error_handling: boolean;
    outputs: { key: string; description: string }[];
    defaults: SettingsFor<K>;
    presets: {
      key: string;
      label: string;
      description: string;
      settings: Partial<SettingsFor<K>>;
    }[];
    max_count: number | null;
  };
}[FlowNode["type"]];
export type AppTemplate = {
  id: string;
  title: string;
  summary: string;
  needs: string[];
  config: FlowConfig;
};
export type AppSource =
  | { blank: true }
  | { template: string }
  | { app: string; version?: number };
export type CreateAppInput = {
  app: string;
  title: string;
  summary?: string;
  from: AppSource;
  actor?: string;
};
export type ImportAppInput = {
  app: string;
  title?: string;
  summary?: string;
  yaml: string;
  actor?: string;
};
export type ImportProblem =
  | { kind: "skill"; name: string; version: number; message: string }
  | {
      kind: "datasource";
      datasource_id: string;
      name?: string;
      message: string;
    }
  | { kind: "knowledge_base"; kb_id: string; name?: string; message: string }
  | { kind: "model" | "secret"; name: string; message: string };
export type ImportResult = { app: AppDetail; problems: ImportProblem[] };
export type NodeTest = {
  status: "done" | "error";
  outputs: Record<string, unknown> | null;
  branch: string | null;
  error: { type: string; message: string } | null;
  duration_ms: number;
  rendered: Record<string, unknown>;
};
export const APP_ID_PATTERN = /^[a-z][a-z0-9-]{1,63}$/;
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
type SettingBase = { key: string; label: string; help?: string };
export type Setting = SettingBase &
  (
    | { type: "model" | "skills" | "boolean" | "knowledge_bases" }
    | { type: "integer"; min?: number; max?: number }
    | { type: "enum"; choices: { value: string; label: string }[] }
    | { type: "string"; max_length?: number }
  );
export type AppNode = {
  id: string;
  title: string;
  kind: NodeKind;
  design: NodeDesign | null;
  settings: Setting[];
};
export type AppEval = {
  status: "running" | "passed" | "failed" | "error";
  score: number | null;
  threshold: number;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  cases: {
    id: string;
    status: string;
    score: number | null;
    run_id: string | null;
    error: string | null;
  }[];
};
export type AppVersion = {
  version: number | null;
  status: "current" | "draft" | "previous";
  config: AppConfig;
  note: string | null;
  created_by: string | null;
  created_at: string;
  eval: AppEval | null;
};
export type AppSummary = {
  app: string;
  title: string;
  summary: string;
  current_version: number;
  has_draft: boolean;
  has_eval_set: boolean;
  kind: "code" | "flow";
  created_at: string | null;
};
export type AppDetail = AppSummary & {
  pattern: string;
  knowledge: string;
  skills_text: string;
  nodes: AppNode[];
  edges: FlowEdge[];
  current: AppVersion;
  draft: AppVersion | null;
  inputs: Input[] | null;
  secrets: { name: string; set: boolean; updated_at: string | null }[];
  options: {
    knowledge_bases: KnowledgeOption[];
    datasources: { datasource_id: string; name: string; kind: string }[];
    models: string[];
    rerank_models: RerankModel[];
    default_model: string;
    skills: {
      name: string;
      description: string;
      published_version: number | null;
    }[];
  };
};

function isSummary(
  value: unknown,
): value is AppSummary & Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.app === "string" &&
    typeof value.title === "string" &&
    typeof value.summary === "string" &&
    Number.isInteger(value.current_version) &&
    typeof value.has_draft === "boolean" &&
    typeof value.has_eval_set === "boolean" &&
    (value.kind === "code" || value.kind === "flow") &&
    (value.created_at === null || typeof value.created_at === "string")
  );
}

function isVersion(value: unknown): value is AppVersion {
  return (
    isRecord(value) &&
    (value.version === null || Number.isInteger(value.version)) &&
    ["current", "draft", "previous"].includes(value.status as string) &&
    isRecord(value.config) &&
    ((value.config.schema_version === 1 &&
      isRecord(value.config.graph) &&
      Array.isArray(value.config.graph.nodes) &&
      Array.isArray(value.config.graph.edges) &&
      isRecord(value.config.env)) ||
      (isRecord(value.config.nodes) &&
        Object.values(value.config.nodes).every(
          (node) =>
            isRecord(node) &&
            (node.model === null || typeof node.model === "string") &&
            Array.isArray(node.skills) &&
            isRecord(node.params),
        ))) &&
    (value.eval === null ||
      (isRecord(value.eval) && Array.isArray(value.eval.cases)))
  );
}

export function readAppDetail(value: unknown): AppDetail {
  if (
    !isSummary(value) ||
    !isRecord(value) ||
    !isVersion(value.current) ||
    !(value.draft === null || isVersion(value.draft)) ||
    !Array.isArray(value.nodes) ||
    !value.nodes.every(
      (node) =>
        isRecord(node) &&
        typeof node.id === "string" &&
        Array.isArray(node.settings),
    ) ||
    !Array.isArray(value.edges) ||
    !isRecord(value.options) ||
    !Array.isArray(value.options.models) ||
    !Array.isArray(value.options.skills)
  ) {
    throw new ApiError("应用详情格式不正确，请稍后重试。");
  }
  return value as AppDetail;
}

const pathFor = (app: string) => `/apps/${encodeURIComponent(app)}`;
export async function listApps(signal?: AbortSignal): Promise<AppSummary[]> {
  const value = await requestJson("/apps", { signal });
  if (!Array.isArray(value) || !value.every(isSummary))
    throw new ApiError("应用列表格式不正确，请稍后重试。");
  return value;
}
export async function getApp(
  app: string,
  signal?: AbortSignal,
): Promise<AppDetail> {
  return readAppDetail(await requestJson(pathFor(app), { signal }));
}
export async function getAppVersions(
  app: string,
  signal?: AbortSignal,
): Promise<AppVersion[]> {
  const value = await requestJson(`${pathFor(app)}/versions`, { signal });
  if (!Array.isArray(value) || !value.every(isVersion))
    throw new ApiError("应用版本格式不正确，请稍后重试。");
  return value;
}
async function changeApp(
  app: string,
  suffix: string,
  method: string,
  body: unknown,
) {
  return readAppDetail(
    await requestJson(`${pathFor(app)}${suffix}`, {
      method,
      body: JSON.stringify(body),
    }),
  );
}
export const saveAppDraft = (app: string, config: AppConfig) =>
  changeApp(app, "/draft", "PUT", { config });
export const deleteAppDraft = (app: string) =>
  requestJson(`${pathFor(app)}/draft`, { method: "DELETE" });
export const evaluateAppDraft = (app: string) =>
  changeApp(app, "/draft/eval", "POST", {});
export const publishAppDraft = (app: string, note: string, actor?: string) =>
  changeApp(app, "/draft/publish", "POST", {
    note,
    ...(actor ? { actor } : {}),
  });
export const activateAppVersion = (
  app: string,
  version: number,
  actor?: string,
) =>
  changeApp(
    app,
    `/versions/${version}/activate`,
    "POST",
    actor ? { actor } : {},
  );

export async function getNodeTypes(signal?: AbortSignal): Promise<NodeType[]> {
  const value = await requestJson("/node-types", { signal });
  if (!Array.isArray(value)) throw new ApiError("节点类型格式不正确。");
  return value as NodeType[];
}
export async function getAppTemplates(
  signal?: AbortSignal,
): Promise<AppTemplate[]> {
  const value = await requestJson("/app-templates", { signal });
  if (!Array.isArray(value)) throw new ApiError("应用模板格式不正确。");
  return value as AppTemplate[];
}
export const createApp = async (input: CreateAppInput): Promise<AppDetail> =>
  readAppDetail(
    await requestJson("/apps", { method: "POST", body: JSON.stringify(input) }),
  );
export const updateApp = (
  app: string,
  input: { title?: string; summary?: string },
) => changeApp(app, "", "PATCH", input);
export const archiveApp = (app: string) =>
  requestJson(pathFor(app), { method: "DELETE" });
export async function importApp(input: ImportAppInput): Promise<ImportResult> {
  const value = await requestJson("/apps/import", {
    method: "POST",
    body: JSON.stringify(input),
  });
  if (!isRecord(value) || !Array.isArray(value.problems))
    throw new ApiError("导入结果格式不正确。");
  return {
    app: readAppDetail(value.app),
    problems: value.problems as ImportProblem[],
  };
}
export async function importAppDraft(
  app: string,
  yaml: string,
  actor?: string,
): Promise<ImportResult> {
  const value = await requestJson(`${pathFor(app)}/draft/import`, {
    method: "POST",
    body: JSON.stringify({ yaml, actor }),
  });
  if (!isRecord(value) || !Array.isArray(value.problems))
    throw new ApiError("导入结果格式不正确。");
  return {
    app: readAppDetail(value.app),
    problems: value.problems as ImportProblem[],
  };
}
export const testAppNode = (
  app: string,
  node: string,
  values: Record<string, unknown>,
  config?: FlowConfig,
) =>
  requestJson(`${pathFor(app)}/nodes/${encodeURIComponent(node)}/test`, {
    method: "POST",
    body: JSON.stringify({ values, config }),
    signal: AbortSignal.timeout(120_000),
  }) as Promise<NodeTest>;
export const setAppSecret = (
  app: string,
  name: string,
  value: string,
  actor?: string,
) =>
  requestJson(`${pathFor(app)}/secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ value, actor }),
  });
export const deleteAppSecret = (app: string, name: string) =>
  requestJson(`${pathFor(app)}/secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
export async function exportAppVersion(
  app: string,
  version: number,
): Promise<void> {
  const response = await fetch(
    `/api${pathFor(app)}/versions/${version}/export`,
    { cache: "no-store" },
  );
  if (!response.ok)
    throw new ApiError("导出应用失败，请重试。", response.status);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  link.download = `${app}-v${version}.yaml`;
  try {
    link.download =
      (encoded ? decodeURIComponent(encoded) : plain) ?? link.download;
  } catch {
    /* Use the application filename for malformed headers. */
  }
  link.href = url;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
