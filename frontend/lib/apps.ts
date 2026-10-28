import type { FlowEdge, NodeDesign, NodeKind } from "./flow";
import { ApiError, isRecord, requestJson } from "./runs";

// docs/api/apps-and-skills.md — names and payloads follow the backend contract.
export type SkillRef = { name: string; version: number };
export type NodeConfig = {
  model: string | null;
  skills: SkillRef[];
  params: Record<string, string | number | boolean>;
};
export type AppConfig = { nodes: Record<string, NodeConfig> };
type SettingBase = { key: string; label: string; help?: string };
export type Setting = SettingBase &
  (
    | { type: "model" | "skills" | "boolean" }
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
};
export type AppDetail = AppSummary & {
  pattern: string;
  knowledge: string;
  skills_text: string;
  nodes: AppNode[];
  edges: FlowEdge[];
  current: AppVersion;
  draft: AppVersion | null;
  options: {
    models: string[];
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
    typeof value.has_eval_set === "boolean"
  );
}

function isVersion(value: unknown): value is AppVersion {
  return (
    isRecord(value) &&
    (value.version === null || Number.isInteger(value.version)) &&
    ["current", "draft", "previous"].includes(value.status as string) &&
    isRecord(value.config) &&
    isRecord(value.config.nodes) &&
    Object.values(value.config.nodes).every(
      (node) =>
        isRecord(node) &&
        (node.model === null || typeof node.model === "string") &&
        Array.isArray(node.skills) &&
        isRecord(node.params),
    ) &&
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
