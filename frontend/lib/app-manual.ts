import type { NodeKind } from "./flow";
import { ApiError, isRecord, requestJson } from "./runs";

export type ManualNode = { id: string; title: string };
export type AppManual = {
  app: string;
  title: string;
  summary: string;
  kind: "flow" | "code";
  version: number | null;
  steps: (ManualNode & {
    type: string | null;
    type_label: string;
    kind: NodeKind;
    note: string | null;
    uses: string[];
  })[];
  skills: {
    name: string;
    version: number;
    description: string;
    nodes: ManualNode[];
  }[];
  knowledge_bases: {
    kb_id: string;
    name: string;
    description: string;
    document_count: number;
    documents: { document_id: string; filename: string; status: string }[];
    nodes: ManualNode[];
  }[];
  datasources: {
    datasource_id: string | null;
    name: string;
    kind: string | null;
    nodes: ManualNode[];
  }[];
  models: { alias: string; nodes: ManualNode[] }[];
  systems: { key: string; name: string; detail: string; nodes: ManualNode[] }[];
  human: (ManualNode & { prompt: string })[];
};

export async function getManual(
  path: string,
  signal?: AbortSignal,
): Promise<AppManual> {
  const value = await requestJson(path, { signal });
  if (
    !isRecord(value) ||
    typeof value.summary !== "string" ||
    !Array.isArray(value.steps) ||
    ![
      "skills",
      "knowledge_bases",
      "datasources",
      "models",
      "systems",
      "human",
    ].every((key) => Array.isArray(value[key]))
  )
    throw new ApiError("应用说明书格式不正确，请稍后重试。");
  return value as AppManual;
}

// Model aliases remain in the manual; canvas badges highlight its resources.
export const resourceUses = (uses: string[]) =>
  uses.filter((use) =>
    /^(Skill|知识库|数据源|Databricks)(?:\s|$|：|:)/.test(use),
  );
