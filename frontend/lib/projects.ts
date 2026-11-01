import { ApiError, isRecord, requestJson, sampleLabels, type SampleLevel } from "./runs";

// docs/api/projects.md is the source of truth for these wire types.
export type Environment = {
  key: string;
  label: string;
  catalog: string;
  schema: string;
  cluster_id: string | null;
};
export type Reference = {
  name: string;
  label: string | null;
  filter: string | null;
  description: string;
};
export type ParamType = "text" | "number" | "boolean" | "date" | "month" | "select";
export type Choice = { value: string; label: string | null };
export type ParamValue = string | number | boolean | null;
export type Param = {
  key: string;
  label: string;
  type: ParamType;
  required: boolean;
  default: ParamValue;
  choices: Choice[] | null;
  description: string;
};
// The project's own setting; null parts follow the platform default.
export type ProjectPolicy = {
  samples: SampleLevel | null;
  models: string[] | null;
};
export type ProjectInput = {
  project_id: string;
  name: string;
  description: string;
  table_prefix: string;
  environments: Environment[];
  references: Reference[];
  adopted: string[];
  params: Param[];
  // Acceptance checks, written like the data check node's (docs/api/data-nodes.md).
  checks: Record<string, unknown>[];
  policy: ProjectPolicy;
};
export type Project = ProjectInput & { created_at: string; updated_at: string };
export type ProjectSummary = {
  project_id: string;
  name: string;
  description: string;
  environments: { key: string; label: string }[];
  updated_at: string;
};
export type ProjectOptions = {
  sample_levels: { value: SampleLevel; label: string; description: string }[];
  // Aliases on the model gateway; null when it could not be read.
  models: string[] | null;
  defaults: { samples: SampleLevel; models: string[] | null };
};
// What a new run binds (POST /runs `project`).
export type ProjectChoice = {
  project_id: string;
  environment?: string;
  params?: Record<string, ParamValue>;
};

export const PROJECT_ID = /^[a-z][a-z0-9-]{1,39}$/;
export const PREFIX = /^[a-z][a-z0-9_]{1,29}$/;
export const KEY = /^[a-z][a-z0-9_]{0,31}$/;
export const TABLE = /^[a-z0-9_]{1,255}$/;
export const THREE_PART = /^[^\s`.]{1,255}\.[^\s`.]{1,255}\.[^\s`.]{1,255}$/;

// What `{{project.<field>}}` may name, with what each holds (docs/api/projects.md).
export const PROJECT_FIELDS = [
  "project_id",
  "name",
  "description",
  "table_prefix",
  "environment",
  "catalog",
  "schema",
  "references",
  "params",
  "checks",
  "brief",
];

export const paramTypeLabels: Record<ParamType, string> = {
  text: "文字",
  number: "数字",
  boolean: "是或否",
  date: "日期",
  month: "月份",
  select: "选项",
};

const base = (id: string) => `/projects/${encodeURIComponent(id)}`;
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export async function listProjects(signal?: AbortSignal) {
  const value = await requestJson("/projects", { signal });
  if (!Array.isArray(value) || !value.every(isSummary))
    throw new ApiError("项目列表格式不正确，请稍后重试。");
  return value;
}
export async function getProject(id: string, signal?: AbortSignal) {
  const value = await requestJson(base(id), { signal });
  if (!isProject(value)) throw new ApiError("项目格式不正确，请稍后重试。");
  return value;
}
export const getProjectOptions = (signal?: AbortSignal) =>
  requestJson("/projects/options", { signal }) as Promise<ProjectOptions>;
export const createProject = (input: ProjectInput) =>
  requestJson("/projects", json("POST", input)) as Promise<Project>;
export const replaceProject = (input: ProjectInput) =>
  requestJson(base(input.project_id), json("PUT", input)) as Promise<Project>;
export const deleteProject = (id: string) =>
  requestJson(base(id), json("DELETE")) as Promise<unknown>;

function isSummary(value: unknown): value is ProjectSummary {
  return (
    isRecord(value) &&
    typeof value.project_id === "string" &&
    typeof value.name === "string" &&
    Array.isArray(value.environments)
  );
}
function isProject(value: unknown): value is Project {
  if (!isRecord(value) || !isSummary(value)) return false;
  const item: Record<string, unknown> = value;
  return (
    typeof item.table_prefix === "string" &&
    Array.isArray(item.references) &&
    Array.isArray(item.adopted) &&
    Array.isArray(item.params) &&
    isRecord(item.policy)
  );
}

// Why `text` cannot be a read filter, or null (the backend's filter_problem): only its
// shape, outside string literals.
export function filterProblem(text: string): string | null {
  if ((text.match(/'/g)?.length ?? 0) % 2) return "读取筛选的引号要配对";
  const bare = text.replace(/'(?:[^']|'')*'/g, "''");
  if (bare.includes(";")) return "读取筛选不能有分号";
  if (bare.includes("--") || bare.includes("/*") || bare.includes("*/"))
    return "读取筛选不能有注释";
  if (/\bselect\b/i.test(bare)) return "读取筛选不能有 SELECT";
  let depth = 0;
  for (const char of bare) {
    depth += char === "(" ? 1 : char === ")" ? -1 : 0;
    if (depth < 0) break;
  }
  return depth ? "读取筛选的括号要配对" : null;
}

function validDate(value: string, month: boolean) {
  const match = month
    ? /^(\d{4})-(\d{2})$/.exec(value)
    : /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const [year, mon, day] = [Number(match[1]), Number(match[2]), Number(match[3] ?? 1)];
  const date = new Date(Date.UTC(year, mon - 1, day));
  return (
    year >= 1 &&
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === mon - 1 &&
    date.getUTCDate() === day
  );
}

// Why `value` does not fit the parameter, or null (the backend's param_problem).
export function paramProblem(
  param: Pick<Param, "label" | "type" | "choices">,
  value: unknown,
): string | null {
  const { label, type } = param;
  if (type === "text" && !(typeof value === "string" && value.length >= 1 && value.length <= 500))
    return `参数「${label}」要写 1–500 个字`;
  if (type === "number" && !(typeof value === "number" && Number.isFinite(value)))
    return `参数「${label}」要是数字`;
  if (type === "boolean" && typeof value !== "boolean")
    return `参数「${label}」要是 true 或 false`;
  if (type === "date" && !(typeof value === "string" && validDate(value, false)))
    return `参数「${label}」要写成 YYYY-MM-DD`;
  if (type === "month" && !(typeof value === "string" && validDate(value, true)))
    return `参数「${label}」要写成 YYYY-MM`;
  if (type === "select") {
    const values = (param.choices ?? []).map((choice) => choice.value);
    if (!values.includes(value as string))
      return `参数「${label}」要是这些之一：${values.join("、")}`;
  }
  return null;
}

// A parameter value as the run page and the form show it.
export function paramText(param: Pick<Param, "type" | "choices"> | undefined, value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  const choice = param?.choices?.find((item) => item.value === value);
  return choice?.label ?? String(value);
}

export const samplesText = (samples: SampleLevel | null, defaults?: SampleLevel) =>
  samples
    ? sampleLabels[samples]
    : `平台默认${defaults ? `（${sampleLabels[defaults]}）` : ""}`;

export function emptyProject(): ProjectInput {
  return {
    project_id: "",
    name: "",
    description: "",
    table_prefix: "",
    environments: [
      { key: "dev", label: "开发", catalog: "", schema: "", cluster_id: null },
      { key: "prod", label: "生产", catalog: "", schema: "", cluster_id: null },
    ],
    references: [],
    adopted: [],
    params: [],
    checks: [],
    policy: { samples: null, models: null },
  };
}

// A project as the form edits it again (the timestamps are not sent back).
export function projectInput(project: Project): ProjectInput {
  return {
    project_id: project.project_id,
    name: project.name,
    description: project.description,
    table_prefix: project.table_prefix,
    environments: project.environments.map((env) => ({ ...env })),
    references: project.references.map((ref) => ({ ...ref })),
    adopted: [...project.adopted],
    params: project.params.map((param) => ({
      ...param,
      choices: param.choices ? param.choices.map((choice) => ({ ...choice })) : null,
    })),
    checks: (project.checks ?? []).map((check) => ({ ...check })),
    policy: {
      samples: project.policy.samples ?? null,
      models: project.policy.models ? [...project.policy.models] : null,
    },
  };
}
