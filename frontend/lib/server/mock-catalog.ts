import "server-only";
import type {
  CatalogItem,
  CatalogKind,
  CatalogQuality,
  CatalogUse,
} from "../catalog";
import { catalogKinds, kindLabel, MAX_TAG, MAX_TAGS } from "../catalog";
import { isRecord } from "../runs";
import {
  appsReferencingKnowledge,
  mockCatalogApps,
  mockDatasourceUsage,
  mockSkillUsage,
} from "./mock-apps";
import { mockError, mockJson, readBody } from "./mock-assets";
import { allMockSources } from "./mock-datasources";
import { mockEvalSetOf } from "./mock-evals";
import { mockTemplates } from "./mock-flow-apps";
import { mockKnowledgeList } from "./mock-knowledge";
import { mockSkillList } from "./mock-skills";

// The asset catalog (docs/api/catalog.md), assembled from the other mocks. Owners, tags
// and pull requests are kept in memory; proposing opens PRs in a pretend repository.
type Meta = {
  owner: string | null;
  tags: string[];
  repo: CatalogItem["repo"];
};
const holder = globalThis as typeof globalThis & {
  aepMockCatalog?: { meta: Map<string, Meta>; pulls: Map<string, number> };
};
const state = (holder.aepMockCatalog ??= { meta: new Map(), pulls: new Map() });
const REPO = "acme/aep-assets";

type Raw = Omit<CatalogItem, "owner" | "owner_set" | "tags" | "repo"> & {
  creator: string | null;
};
const quality = (
  score: number | null,
  basis: string,
  detail = "",
): CatalogQuality => ({ score, basis, detail });

function rawItems(): Raw[] {
  const apps = mockCatalogApps();
  const titles = new Map(apps.map((app) => [app.app, app]));
  const user = (app: string, via: string): CatalogUse => ({
    app,
    title: titles.get(app)?.title ?? app,
    version: titles.get(app)?.version ?? null,
    via,
  });
  const base = { updated_at: null, facts: {}, can_propose: false };
  const items: Raw[] = [];
  for (const t of mockTemplates)
    items.push({
      ...base,
      kind: "template",
      id: t.id,
      title: t.title,
      summary: t.summary,
      link: "/apps",
      version: null,
      used_by: apps
        .filter((app) => app.template === t.title)
        .map((app) => user(app.app, "从模板新建")),
      quality: quality(null, "模板本身不评测"),
      facts: { needs: t.needs },
      creator: "平台内置",
    });
  for (const app of apps)
    items.push({
      ...base,
      kind: "app",
      id: app.app,
      title: app.title,
      summary: app.summary,
      link: `/apps/${app.app}`,
      version: app.version,
      updated_at: app.updated_at,
      used_by: apps.flatMap((other) =>
        other.calls
          .filter((call) => call.app === app.app)
          .map((call) => user(other.app, call.via)),
      ),
      quality:
        app.score !== null
          ? quality(app.score, "评测通过率")
          : quality(null, "还没有评测，近 30 天运行不到 3 次"),
      facts: { kind: app.kind, runs_30d: 0, success_rate: null },
      can_propose: app.kind === "flow",
      creator: app.created_by,
    });
  for (const skill of mockSkillList()) {
    const latest = skill.versions[0];
    items.push({
      ...base,
      kind: "skill",
      id: skill.name,
      title: skill.name,
      summary: (skill.draft ?? latest)?.description ?? "",
      link: `/skills/${skill.name}`,
      version: latest?.version ?? null,
      updated_at: skill.updated_at,
      used_by: mockSkillUsage(skill.name).map((found) =>
        user(found.app, found.node),
      ),
      quality: quality(null, "用到它的应用还没有评测"),
      facts: { versions: skill.versions.length },
      can_propose: true,
      creator: skill.versions.at(-1)?.created_by ?? null,
    });
  }
  for (const kb of mockKnowledgeList()) {
    const failed = kb.status_counts.error;
    items.push({
      ...base,
      kind: "knowledge",
      id: kb.kb_id,
      title: kb.name,
      summary: kb.description,
      link: `/knowledge/${kb.kb_id}`,
      version: null,
      updated_at: kb.updated_at,
      used_by: appsReferencingKnowledge(kb.kb_id).map((app) => user(app, "")),
      quality: kb.regression.last_run
        ? quality(
            kb.regression.last_run.score,
            "检索回归通过率",
            `${kb.regression.last_run.total} 条用例，通过 ${kb.regression.last_run.passed} 条${kb.regression.stale ? "，文档改过还没重跑" : ""}`,
          )
        : quality(
            kb.document_count
              ? kb.status_counts.ready / kb.document_count
              : null,
            "文档处理成功率",
            `${kb.document_count} 个文档${failed ? `，${failed} 个处理失败` : ""}`,
          ),
      facts: {
        documents: kb.document_count,
        chunks: kb.chunk_count,
        embedding_model: kb.embedding_model,
      },
      can_propose: true,
      creator: null,
    });
  }
  for (const source of allMockSources())
    items.push({
      ...base,
      kind: "tool",
      id: source.datasource_id,
      title: source.name,
      summary: source.description,
      link: "/datasources",
      version: null,
      updated_at: source.created_at,
      used_by: mockDatasourceUsage(source.datasource_id).map((found) =>
        user(found.app, found.via === "literal" ? "" : "开始节点的默认值"),
      ),
      quality: quality(null, "用到它的应用近 30 天运行不到 3 次"),
      facts: { type: source.kind },
      creator: null,
    });
  for (const app of apps) {
    const set = mockEvalSetOf(app.app);
    if (!set?.cases.length) continue;
    items.push({
      ...base,
      kind: "eval_set",
      id: app.app,
      title: `${app.title}的评测集`,
      summary: `${set.cases.length} 条用例`,
      link: `/apps/${app.app}#release`,
      version: null,
      updated_at: set.updated_at,
      used_by: [user(app.app, "评测集")],
      quality:
        app.score !== null
          ? quality(app.score, "最近一次评测通过率", `门槛 ${set.threshold}`)
          : quality(null, "当前版本还没有评测"),
      facts: { cases: set.cases.length, threshold: set.threshold },
      can_propose: set.source === "platform",
      creator: set.updated_by,
    });
  }
  const order = catalogKinds.map(([kind]) => kind);
  return items.sort(
    (a, b) =>
      order.indexOf(a.kind) - order.indexOf(b.kind) ||
      a.title.localeCompare(b.title, "zh"),
  );
}

const keyOf = (kind: string, id: string) => `${kind}:${id}`;

function items(): CatalogItem[] {
  const raw = rawItems();
  const live = new Set(raw.map((item) => keyOf(item.kind, item.id)));
  for (const key of state.meta.keys())
    if (!live.has(key)) state.meta.delete(key);
  return raw.map(({ creator, ...item }) => {
    const meta = state.meta.get(keyOf(item.kind, item.id));
    return {
      ...item,
      owner: meta?.owner || creator || null,
      owner_set: Boolean(meta?.owner),
      tags: meta?.tags ?? [],
      repo: meta?.repo ?? null,
    };
  });
}

function find(kind: string, id: string) {
  return items().find((item) => item.kind === kind && item.id === id);
}

export async function mockCatalog(request: Request, path: string[]) {
  if (path.length === 1) {
    const url = new URL(request.url);
    const kind = url.searchParams.get("kind");
    const q = url.searchParams.get("q")?.trim().toLowerCase();
    if (kind && !catalogKinds.some(([key]) => key === kind))
      return mockError(`不认识的类别 ${kind}`, 422);
    return mockJson({
      items: items().filter(
        (item) =>
          (!kind || item.kind === kind) &&
          (!q ||
            [item.title, item.summary, item.owner ?? "", ...item.tags].some(
              (text) => text.toLowerCase().includes(q),
            )),
      ),
      repo_ready: true,
    });
  }
  const [, kind, id] = path;
  const item = find(kind, id);
  if (!item) return mockError("没有这项资产", 404);
  const key = keyOf(kind, id);
  const meta = state.meta.get(key) ?? { owner: null, tags: [], repo: null };
  if (path.length === 3 && request.method === "GET") return mockJson(item);
  // Proposing takes an optional body, as the backend does.
  const read: unknown =
    path.length === 4
      ? await request.json().catch(() => ({}))
      : await readBody(request);
  if (read instanceof Response) return read;
  const body = isRecord(read) ? read : {};
  if (path.length === 3) {
    const owner = typeof body.owner === "string" ? body.owner.trim() : "";
    if (owner.length > 50) return mockError("负责人最多 50 个字符", 422);
    const tags: string[] = [];
    for (const tag of Array.isArray(body.tags) ? body.tags : []) {
      const text = typeof tag === "string" ? tag.trim() : "";
      if (!text || text.length > MAX_TAG)
        return mockError(`每个标签 1–${MAX_TAG} 个字符`, 422);
      if (!tags.includes(text)) tags.push(text);
    }
    if (tags.length > MAX_TAGS)
      return mockError(`标签最多 ${MAX_TAGS} 个`, 422);
    state.meta.set(key, { ...meta, owner: owner || null, tags });
    return mockJson(find(kind, id));
  }
  if (!item.can_propose) {
    if (kind === "app")
      return mockError(`「${item.title}」是代码应用，不提交到资产仓库`, 409);
    if (kind === "eval_set")
      return mockError(`「${item.title}」放在代码里，不提交到资产仓库`, 409);
    return mockError(`${kindLabel(kind as CatalogKind)}不提交到资产仓库`, 409);
  }
  if ((kind === "app" || kind === "skill") && item.version === null)
    return mockError(`「${item.title}」还没有发布的版本`, 409);
  const existing = state.pulls.get(key);
  const number = existing ?? state.pulls.size + 1;
  state.pulls.set(key, number);
  const url = `https://github.com/${REPO}/pull/${number}`;
  const actor = typeof body.actor === "string" ? body.actor : null;
  state.meta.set(key, {
    ...meta,
    repo: { number, url, at: new Date().toISOString(), by: actor },
  });
  const directory =
    kind === "app"
      ? `apps/${id}`
      : kind === "skill"
        ? `skills/${id}`
        : kind === "knowledge"
          ? `knowledge/${id}`
          : `evals/${id}`;
  const file =
    kind === "app"
      ? "app.yaml"
      : kind === "skill"
        ? "SKILL.md"
        : kind === "knowledge"
          ? "README.md"
          : "eval-set.yaml";
  return mockJson(
    {
      number,
      url,
      branch: `aep/catalog/${kind}/${id}`,
      created: existing === undefined,
      changed: [`${directory}/${file}`],
    },
    201,
  );
}
