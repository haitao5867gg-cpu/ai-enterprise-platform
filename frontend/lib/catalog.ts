import { requestJson } from "./runs";

// docs/api/catalog.md is the source of truth for these wire types.
export type CatalogKind =
  | "template"
  | "app"
  | "skill"
  | "knowledge"
  | "tool"
  | "eval_set";
export const catalogKinds: [CatalogKind, string][] = [
  ["template", "应用模板"],
  ["app", "应用"],
  ["skill", "Skill"],
  ["knowledge", "知识库"],
  ["tool", "数据源和工具"],
  ["eval_set", "评测集"],
];
export const kindLabel = (kind: CatalogKind) =>
  catalogKinds.find(([key]) => key === kind)?.[1] ?? kind;

export type CatalogUse = {
  app: string;
  title: string;
  version: number | null;
  via: string;
};
export type CatalogQuality = {
  score: number | null;
  basis: string;
  detail: string;
};
export type CatalogRepo = {
  number: number;
  url: string;
  at: string;
  by: string | null;
};
export type CatalogItem = {
  kind: CatalogKind;
  id: string;
  title: string;
  summary: string;
  link: string;
  owner: string | null;
  owner_set: boolean;
  tags: string[];
  version: number | null;
  updated_at: string | null;
  used_by: CatalogUse[];
  quality: CatalogQuality;
  facts: Record<string, unknown>;
  repo: CatalogRepo | null;
  can_propose: boolean;
};
export type CatalogList = { items: CatalogItem[]; repo_ready: boolean };
export type Proposed = {
  number: number;
  url: string;
  branch: string;
  created: boolean;
  changed: string[];
};

export const MAX_TAGS = 10;
export const MAX_TAG = 20;

const path = (item: Pick<CatalogItem, "kind" | "id">) =>
  `/catalog/${item.kind}/${encodeURIComponent(item.id)}`;

export const listCatalog = (signal?: AbortSignal) =>
  requestJson("/catalog", { signal }) as Promise<CatalogList>;

export const setCatalogMeta = (
  item: Pick<CatalogItem, "kind" | "id">,
  owner: string | null,
  tags: string[],
  actor?: string,
) =>
  requestJson(path(item), {
    method: "PUT",
    body: JSON.stringify({ owner, tags, actor }),
  }) as Promise<CatalogItem>;

export const proposeAsset = (
  item: Pick<CatalogItem, "kind" | "id">,
  note: string,
  actor?: string,
) =>
  requestJson(`${path(item)}/pull-request`, {
    method: "POST",
    body: JSON.stringify({ note, actor }),
  }) as Promise<Proposed>;

// "86%"; null scores read 暂无.
export const percent = (score: number | null) =>
  score === null ? "暂无" : `${Math.round(score * 100)}%`;

// Tags typed with commas or 、 between them, trimmed and without repeats.
export function parseTags(text: string): string[] {
  const found: string[] = [];
  for (const part of text.split(/[,，、]/)) {
    const tag = part.trim();
    if (tag && !found.includes(tag)) found.push(tag);
  }
  return found;
}
