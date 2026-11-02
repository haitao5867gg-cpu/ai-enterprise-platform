import "server-only";
import type {
  Chunk,
  DocTemplate,
  Document,
  DocumentVersion,
  Hit,
  KnowledgeBase,
  KnowledgeOptions,
  Regression,
  RegressionCase,
  Retrieval,
  Validity,
} from "../knowledge";
import { MAX_CASES, uploadProblem } from "../knowledge";
import { retrievalProblems, retrievalValue } from "../retrieval";
import { isRecord } from "../runs";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";

export const mockKnowledgeOptions: KnowledgeOptions = {
  embedding_models: ["mock-embed", "company-embed"],
  default_embedding_model: "mock-embed",
  chunking: { size: 800, overlap: 100 },
  retrieval: { mode: "hybrid", vector_weight: 0.7, rerank: null },
  rerank_models: [
    { model: "mock-rerank", kind: "rerank" },
    ...["mock", "gpt", "azure-gpt", "claude-bedrock", "qwen", "deepseek"].map(
      (model) => ({ model, kind: "llm" as const }),
    ),
  ],
};
// Stored documents keep the governance fields; validity is worked out on each read.
type StoredDoc = Omit<Document, "validity">;
type StoredDocument = {
  document: StoredDoc;
  chunks: Chunk[];
  bytes: Uint8Array;
  text: string;
  polls: number;
  versions: DocumentVersion[];
};
type StoredKnowledge = {
  base: Omit<KnowledgeBase, "validity_counts">;
  documents: Map<string, StoredDocument>;
  regression?: Omit<Regression, "stale" | "kb_id">;
  changedAt?: string;
};
const now = () => new Date().toISOString();
const governed = {
  owner: null,
  valid_from: null,
  valid_until: null,
  version: 1,
  uploaded_by: null,
  hits_30d: 0,
  last_hit_at: null,
} as const;
const firstVersion = (doc: Pick<Document, "size_bytes" | "created_at">) => [
  {
    version: 1,
    size_bytes: doc.size_bytes,
    uploaded_by: null,
    uploaded_at: doc.created_at,
  },
];
// The platform timezone's today, as the backend counts validity.
const today = () =>
  new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
function validity(doc: Pick<Document, "valid_from" | "valid_until">): Validity {
  const day = today();
  if (doc.valid_from && doc.valid_from > day) return "pending";
  if (doc.valid_until && doc.valid_until < day) return "expired";
  return "active";
}
const view = (doc: StoredDoc): Document => ({
  ...doc,
  validity: validity(doc),
});
const globalStore = globalThis as typeof globalThis & {
  aepMockKnowledge?: Map<string, StoredKnowledge>;
};
const store: Map<string, StoredKnowledge> = (globalStore.aepMockKnowledge ??=
  new Map());
const sampleText =
  "退换货期限为签收后七天，请保留原包装和购买凭证。申请通过后原路退还货款，特殊商品按售后政策处理。";
if (!store.size) {
  const kb_id = "policies",
    document_id = "return-policy";
  const chunks: Chunk[] = Array.from({ length: 61 }, (_, position) => ({
    chunk_id: `policy-chunk-${position}`,
    document_id,
    position,
    text:
      position === 55
        ? `第七章 退货期限：${sampleText}`
        : `${sampleText} 资料条目 ${position + 1}。`,
    heading:
      position === 55 ? "第七章 售后 > 7.1 退换货期限" : "售后政策 > 退换货",
    page: position + 1,
  }));
  store.set(kb_id, {
    base: {
      kb_id,
      name: "售后政策",
      description: "退换货、维修及服务政策，供客服和交付团队检索。",
      embedding_model: "mock-embed",
      chunking: { size: 800, overlap: 100 },
      document_count: 1,
      chunk_count: 61,
      status_counts: { queued: 0, processing: 0, ready: 1, error: 0 },
      created_at: now(),
      updated_at: now(),
    },
    documents: new Map([
      [
        document_id,
        {
          document: {
            document_id,
            kb_id,
            name: "退换货政策.pdf",
            kind: "pdf",
            size_bytes: 1024,
            status: "ready",
            error: null,
            chunk_count: 61,
            created_at: now(),
            updated_at: now(),
            ...governed,
            owner: "售后组",
            hits_30d: 12,
            last_hit_at: now(),
          },
          chunks,
          text: sampleText,
          bytes: new TextEncoder().encode("%PDF-1.4\nMock policy document\n"),
          polls: 3,
          versions: firstVersion({ size_bytes: 1024, created_at: now() }),
        },
      ],
    ]),
  });
}
function snapshot(entry: StoredKnowledge): KnowledgeBase {
  const documents = [...entry.documents.values()].map((item) => item.document);
  const states = documents.map(validity);
  return {
    ...entry.base,
    validity_counts: {
      active: states.filter((state) => state === "active").length,
      pending: states.filter((state) => state === "pending").length,
      expired: states.filter((state) => state === "expired").length,
    },
    document_count: documents.length,
    chunk_count: documents.reduce((sum, doc) => sum + doc.chunk_count, 0),
    status_counts: {
      queued: documents.filter((doc) => doc.status === "queued").length,
      processing: documents.filter((doc) => doc.status === "processing").length,
      ready: documents.filter((doc) => doc.status === "ready").length,
      error: documents.filter((doc) => doc.status === "error").length,
    },
  };
}
for (const [id, name, filenames] of [
  [
    "retail-metrics",
    "零售数据口径",
    [
      "01-有效订单定义.md",
      "02-销售额与客单价.md",
      "03-城市与会员等级.md",
      "04-时间口径.md",
      "05-客户数与复购.md",
      "06-毛利.md",
    ],
  ],
  [
    "dw-standards",
    "数仓开发规范",
    [
      "01-分层与命名.md",
      "02-主键与字段.md",
      "03-数据质量规则.md",
      "04-加载策略.md",
      "05-PySpark编码规范.md",
    ],
  ],
] as const) {
  if (store.has(id)) continue;
  const text =
    id === "retail-metrics"
      ? "有效订单包含 paid、shipped、completed；排除测试账号，统一城市名称后统计销售额。"
      : "按数仓分层命名，明确主键、加载策略和质量规则，用 PySpark 编写转换。";
  store.set(id, {
    base: {
      kb_id: id,
      name,
      description: text,
      embedding_model: "mock-embed",
      chunking: { size: 800, overlap: 100 },
      document_count: filenames.length,
      chunk_count: filenames.length,
      status_counts: {
        queued: 0,
        processing: 0,
        ready: filenames.length,
        error: 0,
      },
      created_at: now(),
      updated_at: now(),
    },
    documents: new Map(
      filenames.map((filename, i) => {
        const document_id = `${id}-${i + 1}`;
        return [
          document_id,
          {
            document: {
              document_id,
              kb_id: id,
              name: filename,
              kind: "md" as const,
              size_bytes: new TextEncoder().encode(text).length,
              status: "ready" as const,
              error: null,
              chunk_count: 1,
              created_at: now(),
              updated_at: now(),
              ...governed,
            },
            versions: firstVersion({
              size_bytes: new TextEncoder().encode(text).length,
              created_at: now(),
            }),
            chunks: [
              {
                chunk_id: `${document_id}-chunk`,
                document_id,
                position: 0,
                text,
                heading: filename,
                page: null,
              },
            ],
            text,
            bytes: new TextEncoder().encode(text),
            polls: 3,
          },
        ];
      }),
    ),
  });
}
export function mockKnowledgeManual(id: string) {
  const entry = store.get(id);
  return {
    kb_id: id,
    name: entry?.base.name ?? id,
    description: entry?.base.description ?? "已删除",
    document_count: entry?.documents.size ?? 0,
    documents: entry
      ? [...entry.documents.values()].map(({ document }) => ({
          document_id: document.document_id,
          filename: document.name,
          status: document.status,
        }))
      : [],
  };
}
export const knowledgeOptions = () =>
  [...store.values()]
    .map(snapshot)
    .sort((a, b) => a.name.localeCompare(b.name))
    .map(({ kb_id, name, document_count }) => ({
      kb_id,
      name,
      document_count,
    }));
export const knowledgeExists = (id: string) => store.has(id);
export function searchMockKnowledge(
  ids: string[],
  query: string,
  top_k = 5,
  min_score = 0.3,
  retrieval?: Retrieval,
  includeInactive = false,
): Hit[] {
  if (!query.trim()) return [];
  const settings = retrievalValue(retrieval);
  if (settings.rerank && query.includes("重排失败"))
    throw new Error("重排失败：模拟重排服务不可用，请稍后重试");
  const hits = ids
    .flatMap((id) => {
      const entry = store.get(id);
      if (!entry) return [];
      return [...entry.documents.values()]
        .filter(
          (item) =>
            item.document.status === "ready" &&
            (includeInactive || validity(item.document) === "active"),
        )
        .flatMap((item) =>
          item.chunks.map((chunk) => {
            const vector =
              chunk.position === 55
                ? 0.93
                : Math.max(0.35, 0.86 - chunk.position * 0.005);
            const keyword =
              chunk.position === 55
                ? 0.75
                : Math.max(0.25, 0.65 - chunk.position * 0.005);
            const scores = {
              vector: settings.mode === "keyword" ? null : vector,
              keyword: settings.mode === "vector" ? null : keyword,
              rerank: null as number | null,
            };
            return {
              ...chunk,
              kb_id: id,
              kb_name: entry.base.name,
              document_name: item.document.name,
              scores,
              score:
                settings.mode === "vector"
                  ? vector
                  : settings.mode === "keyword"
                    ? keyword
                    : settings.vector_weight * vector +
                      (1 - settings.vector_weight) * keyword,
            };
          }),
        );
    })
    .sort((a, b) => b.score - a.score || a.chunk_id.localeCompare(b.chunk_id));
  const ranked = settings.rerank
    ? hits
        .slice(0, settings.rerank.candidates ?? Math.max(20, top_k))
        .map((hit) => {
          const rerank =
            hit.position === 55
              ? 0.91
              : Math.max(0.2, 0.8 - hit.position * 0.007);
          return { ...hit, score: rerank, scores: { ...hit.scores, rerank } };
        })
    : hits;
  return ranked
    .filter((hit) => hit.score >= min_score)
    .sort((a, b) => b.score - a.score || a.chunk_id.localeCompare(b.chunk_id))
    .slice(0, top_k);
}

function validateBase(
  body: Record<string, unknown>,
  entry?: StoredKnowledge,
): Response | null {
  if (
    (!entry || "name" in body) &&
    (typeof body.name !== "string" ||
      body.name.trim().length < 1 ||
      body.name.trim().length > 50)
  )
    return mockError("名称须为 1–50 字符", 422);
  if (
    typeof body.name === "string" &&
    [...store.values()].some(
      (item) =>
        item !== entry && item.base.name === (body.name as string).trim(),
    )
  )
    return mockError("知识库名称已存在", 409);
  if (
    "description" in body &&
    (typeof body.description !== "string" || body.description.length > 500)
  )
    return mockError("说明最多 500 字符", 422);
  if (
    "embedding_model" in body &&
    !mockKnowledgeOptions.embedding_models.includes(
      body.embedding_model as string,
    )
  )
    return mockError("没有这个向量模型别名", 422);
  if (
    entry?.documents.size &&
    body.embedding_model &&
    body.embedding_model !== entry.base.embedding_model
  )
    return mockError("已有文档，不能更改向量模型", 409);
  if ("chunking" in body) {
    const chunking = body.chunking;
    if (
      !isRecord(chunking) ||
      typeof chunking.size !== "number" ||
      !Number.isInteger(chunking.size) ||
      chunking.size < 200 ||
      chunking.size > 2000 ||
      typeof chunking.overlap !== "number" ||
      !Number.isInteger(chunking.overlap) ||
      chunking.overlap < 0 ||
      chunking.overlap > chunking.size / 2
    )
      return mockError(
        "切分大小须为 200–2000 的整数，重叠须为 0 到大小一半的整数",
        422,
      );
  }
  return null;
}
function queue(item: StoredDocument) {
  item.polls = 0;
  Object.assign(item.document, {
    status: "queued",
    error: null,
    chunk_count: 0,
    updated_at: now(),
  });
  item.chunks = [];
}
function tick(entry: StoredKnowledge) {
  for (const item of entry.documents.values()) {
    if (!["queued", "processing"].includes(item.document.status)) continue;
    item.polls++;
    item.document.updated_at = now();
    if (item.polls <= 2) {
      item.document.status = "processing";
      continue;
    }
    if (item.text.trim().length < 20) {
      item.document.status = "error";
      item.document.error = "没有可提取的文字（可能是扫描件）";
      continue;
    }
    const { size, overlap } = entry.base.chunking;
    for (let offset = 0; offset < item.text.length; offset += size - overlap) {
      const text = item.text.slice(offset, offset + size);
      if (text.trim().length < 20) continue;
      const position = item.chunks.length;
      item.chunks.push({
        chunk_id: `${item.document.document_id}-${position}`,
        document_id: item.document.document_id,
        position,
        text,
        heading:
          item.document.kind === "md"
            ? (item.text.match(/^#+\s+(.+)/m)?.[1] ?? "")
            : "",
        page: item.document.kind === "pdf" ? position + 1 : null,
      });
    }
    item.document.status = "ready";
    item.document.chunk_count = item.chunks.length;
  }
}
export async function mockKnowledge(
  request: Request,
  path: string[],
): Promise<Response> {
  const [, id, section, docId, action] = path;
  const method = request.method;
  if (id === "options") return mockJson(mockKnowledgeOptions);
  if (id === "doc-templates") return mockJson(docTemplates);
  if (!id && method === "GET")
    return mockJson(
      [...store.values()]
        .map(snapshot)
        .sort((a, b) => a.name.localeCompare(b.name)),
    );
  if (!id && method === "POST") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const invalid = validateBase(body);
    if (invalid) return invalid;
    const kb_id = crypto.randomUUID();
    const base: StoredKnowledge["base"] = {
      kb_id,
      name: (body.name as string).trim(),
      description: (body.description as string) ?? "",
      embedding_model:
        (body.embedding_model as string) ??
        mockKnowledgeOptions.default_embedding_model,
      chunking: (body.chunking as KnowledgeBase["chunking"]) ?? {
        ...mockKnowledgeOptions.chunking,
      },
      document_count: 0,
      chunk_count: 0,
      status_counts: { queued: 0, processing: 0, ready: 0, error: 0 },
      created_at: now(),
      updated_at: now(),
    };
    const created: StoredKnowledge = { base, documents: new Map() };
    store.set(kb_id, created);
    return mockJson(snapshot(created), 201);
  }
  const entry = store.get(id);
  if (!entry) return mockError("没有这个知识库", 404);
  if (!section) {
    if (method === "GET") return mockJson(snapshot(entry));
    if (method === "DELETE") {
      const { appsReferencingKnowledge } = await import("./mock-apps");
      const apps = appsReferencingKnowledge(id);
      if (apps.length)
        return mockError(
          `以下应用的当前版本正在使用此知识库：${apps.join("、")}`,
          409,
        );
      store.delete(id);
      return noContent();
    }
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const invalid = validateBase(body, entry);
    if (invalid) return invalid;
    for (const key of [
      "name",
      "description",
      "embedding_model",
      "chunking",
    ] as const)
      if (key in body)
        Object.assign(entry.base, {
          [key]: key === "name" ? (body.name as string).trim() : body[key],
        });
    entry.base.updated_at = now();
    return mockJson(snapshot(entry));
  }
  if (section === "reindex") {
    for (const item of entry.documents.values()) queue(item);
    return mockJson({ queued: entry.documents.size }, 202);
  }
  if (section === "search") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const {
      query,
      top_k = 5,
      min_score = 0.3,
      retrieval,
      include_inactive = false,
    } = body;
    if (
      typeof query !== "string" ||
      !query.trim() ||
      query.length > 2000 ||
      typeof top_k !== "number" ||
      !Number.isInteger(top_k) ||
      top_k < 1 ||
      top_k > 20 ||
      typeof min_score !== "number" ||
      min_score < 0 ||
      min_score > 1
    )
      return mockError(
        "问题须为 1–2000 字符，top_k 为 1–20，min_score 为 0–1",
        422,
      );
    const problems = Object.values(
      retrievalProblems(retrieval, top_k, mockKnowledgeOptions.rerank_models),
    );
    if (problems.length) return mockError(problems.join("；"), 422);
    try {
      return mockJson({
        hits: searchMockKnowledge(
          [id],
          query,
          top_k,
          min_score,
          retrieval as Retrieval | undefined,
          include_inactive === true,
        ),
        duration_ms: 12,
      });
    } catch (error) {
      return mockError(
        error instanceof Error ? error.message : "检索失败",
        502,
      );
    }
  }
  if (section === "regression") return regression(request, entry, docId);
  if (!docId) {
    if (method === "GET") {
      tick(entry);
      return mockJson(
        [...entry.documents.values()]
          .map((item) => view(item.document))
          .reverse(),
      );
    }
    let form: FormData;
    try {
      form = await request.formData();
    } catch {
      return mockError("无法读取上传表单", 422);
    }
    const files = form
      .getAll("files")
      .filter((file): file is File => file instanceof File);
    const problem = uploadProblem(files);
    if (problem) return mockError(problem, 422);
    const replace = form.get("replace") === "true";
    const uploadedBy =
      typeof form.get("uploaded_by") === "string"
        ? (form.get("uploaded_by") as string).trim() || null
        : null;
    const byName = new Map(
      [...entry.documents.values()].map((item) => [item.document.name, item]),
    );
    if (replace) {
      const names = files.map((file) => file.name);
      const twice = names.find((name, i) => names.indexOf(name) !== i);
      if (twice)
        return mockError(
          `这次上传里有两个「${twice}」，替换时每个文件名只能传一个`,
          422,
        );
    }
    const fresh = files.filter((file) => !(replace && byName.has(file.name)));
    if (entry.documents.size + fresh.length > 500)
      return mockError("一个知识库最多 500 个文档", 422);
    const result: Document[] = [];
    entry.changedAt = now();
    for (const file of files) {
      const document_id = crypto.randomUUID();
      const kind = file.name
        .split(".")
        .at(-1)!
        .toLowerCase() as Document["kind"];
      const old = replace ? byName.get(file.name) : undefined;
      if (old) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        old.bytes = bytes;
        old.text = ["md", "txt"].includes(kind)
          ? new TextDecoder().decode(bytes)
          : sampleText;
        queue(old);
        Object.assign(old.document, {
          size_bytes: file.size,
          version: old.document.version + 1,
          uploaded_by: uploadedBy,
        });
        old.versions.unshift({
          version: old.document.version,
          size_bytes: file.size,
          uploaded_by: uploadedBy,
          uploaded_at: now(),
        });
        result.push(view(old.document));
        continue;
      }
      let name = file.name,
        suffix = 2;
      while (
        [...entry.documents.values()].some(
          (item) => item.document.name === name,
        )
      )
        name = file.name.replace(/(\.[^.]+)$/, ` (${suffix++})$1`);
      const bytes = new Uint8Array(await file.arrayBuffer());
      const text = ["md", "txt"].includes(kind)
        ? new TextDecoder().decode(bytes)
        : sampleText;
      const document: StoredDoc = {
        document_id,
        kb_id: id,
        name,
        kind,
        size_bytes: file.size,
        status: "queued",
        error: null,
        chunk_count: 0,
        created_at: now(),
        updated_at: now(),
        ...governed,
        uploaded_by: uploadedBy,
      };
      entry.documents.set(document_id, {
        document,
        chunks: [],
        bytes,
        text,
        polls: 0,
        versions: [
          {
            version: 1,
            size_bytes: file.size,
            uploaded_by: uploadedBy,
            uploaded_at: now(),
          },
        ],
      });
      result.push(view(document));
    }
    return mockJson(result, 202);
  }
  const item = entry.documents.get(docId);
  if (!item) return mockError("没有这个文档", 404);
  if (!action) {
    if (method === "DELETE") {
      entry.documents.delete(docId);
      entry.changedAt = now();
      return noContent();
    }
    if (method === "PATCH") {
      const body = await readBody(request);
      if (body instanceof Response) return body;
      const date = /^\d{4}-\d{2}-\d{2}$/;
      for (const key of ["valid_from", "valid_until"] as const)
        if (
          key in body &&
          body[key] !== null &&
          (typeof body[key] !== "string" || !date.test(body[key] as string))
        )
          return mockError("日期要写成 YYYY-MM-DD", 422);
      if (
        "owner" in body &&
        body.owner !== null &&
        (typeof body.owner !== "string" || body.owner.trim().length > 50)
      )
        return mockError("负责人最多 50 个字符", 422);
      const next = {
        valid_from:
          "valid_from" in body
            ? (body.valid_from as string | null)
            : item.document.valid_from,
        valid_until:
          "valid_until" in body
            ? (body.valid_until as string | null)
            : item.document.valid_until,
      };
      if (
        next.valid_from &&
        next.valid_until &&
        next.valid_until < next.valid_from
      )
        return mockError("失效日期不能早于生效日期", 422);
      if ("owner" in body)
        item.document.owner = (body.owner as string | null)?.trim() || null;
      if ("valid_from" in body || "valid_until" in body) {
        Object.assign(item.document, next);
        entry.changedAt = now();
      }
      return mockJson(view(item.document));
    }
    return mockJson(view(item.document));
  }
  if (action === "versions") return mockJson(item.versions);
  if (action === "reindex") {
    queue(item);
    return mockJson(view(item.document), 202);
  }
  if (action === "file")
    return new Response(new Uint8Array(item.bytes), {
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(item.document.name)}`,
        "Cache-Control": "no-store",
      },
    });
  const params = new URL(request.url).searchParams,
    offset = Number(params.get("offset") ?? 0),
    limit = Number(params.get("limit") ?? 50);
  if (
    !Number.isInteger(offset) ||
    offset < 0 ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 200
  )
    return mockError("分页参数不合法", 422);
  return mockJson({
    total: item.chunks.length,
    items: item.chunks.slice(offset, offset + limit),
  });
}

// The asset catalog (docs/api/catalog.md).
export const mockKnowledgeList = () =>
  [...store.values()].map((entry) => ({
    ...snapshot(entry),
    regression: regressionOf(entry),
  }));

function regressionOf(entry: StoredKnowledge): Regression {
  const stored = entry.regression ?? {
    cases: [],
    retrieval: { ...mockKnowledgeOptions.retrieval },
    top_k: 5,
    min_score: 0.3,
    updated_by: null,
    updated_at: null,
    last_run: null,
  };
  const ran = stored.last_run?.at;
  const stale =
    stored.cases.length > 0 &&
    (!ran ||
      (stored.updated_at !== null && stored.updated_at > ran) ||
      (entry.changedAt !== undefined && entry.changedAt > ran));
  return { kb_id: entry.base.kb_id, ...stored, stale };
}

async function regression(
  request: Request,
  entry: StoredKnowledge,
  action?: string,
): Promise<Response> {
  if (request.method === "GET") return mockJson(regressionOf(entry));
  const read: unknown = await request.json().catch(() => ({}));
  const body = isRecord(read) ? read : {};
  const actor =
    typeof body.actor === "string" ? body.actor.trim() || null : null;
  if (action === "run") {
    const current = regressionOf(entry);
    if (!current.cases.length) return mockError("先加检索用例再运行回归", 409);
    const results = current.cases.map((item) => {
      const top = [
        ...new Set(
          searchMockKnowledge(
            [entry.base.kb_id],
            item.query,
            current.top_k,
            current.min_score,
            current.retrieval,
          ).map((hit) => hit.document_name),
        ),
      ];
      const missing = item.expected.filter((name) => !top.includes(name));
      return {
        id: item.id,
        passed: !missing.length,
        found: item.expected.filter((name) => top.includes(name)),
        missing,
        top,
        error: null,
      };
    });
    const passed = results.filter((result) => result.passed).length;
    entry.regression = {
      ...current,
      last_run: {
        at: now(),
        by: actor,
        passed,
        total: results.length,
        score: passed / results.length,
        results,
      },
    };
    return mockJson(regressionOf(entry));
  }
  const cases = Array.isArray(body.cases) ? body.cases : null;
  if (!cases || cases.length > MAX_CASES)
    return mockError(`最多 ${MAX_CASES} 条用例`, 422);
  const names = new Set(
    [...entry.documents.values()].map((item) => item.document.name),
  );
  const seen = new Set<string>();
  const clean: RegressionCase[] = [];
  for (const raw of cases) {
    if (!isRecord(raw)) return mockError("用例格式不对", 422);
    const caseId = typeof raw.id === "string" ? raw.id : "";
    const query = typeof raw.query === "string" ? raw.query.trim() : "";
    const expected = Array.isArray(raw.expected)
      ? raw.expected.filter((name): name is string => typeof name === "string")
      : [];
    if (!/^[a-z0-9_-]{1,40}$/.test(caseId))
      return mockError("用例编号是 1–40 位小写字母、数字、_ 或 -", 422);
    if (seen.has(caseId)) return mockError(`用例编号「${caseId}」重复了`, 422);
    seen.add(caseId);
    if (!query || query.length > 500)
      return mockError("问题 1–500 个字符", 422);
    if (!expected.length || expected.length > 5)
      return mockError("每条用例要选 1–5 个应找到的文档", 422);
    const missing = expected.find((name) => !names.has(name));
    if (missing) return mockError(`知识库里没有文档「${missing}」`, 422);
    clean.push({ id: caseId, query, expected: [...new Set(expected)] });
  }
  const topK = typeof body.top_k === "number" ? body.top_k : 5;
  const minScore = typeof body.min_score === "number" ? body.min_score : 0.3;
  if (!Number.isInteger(topK) || topK < 1 || topK > 20)
    return mockError("top_k 为 1–20", 422);
  if (minScore < 0 || minScore > 1) return mockError("min_score 为 0–1", 422);
  const retrieval = retrievalValue(body.retrieval as Retrieval | undefined);
  const problems = Object.values(
    retrievalProblems(retrieval, topK, mockKnowledgeOptions.rerank_models),
  );
  if (problems.length) return mockError(problems.join("；"), 422);
  entry.regression = {
    cases: clean,
    retrieval,
    top_k: topK,
    min_score: minScore,
    updated_by: actor,
    updated_at: now(),
    last_run: entry.regression?.last_run ?? null,
  };
  return mockJson(regressionOf(entry));
}

// The same templates as the backend's aep.knowledge.governance.DOC_TEMPLATES.
const docTemplates: DocTemplate[] = [
  {
    id: "metric",
    title: "指标口径",
    filename: "指标口径-{名称}.md",
    body: [
      "# {名称}",
      "",
      "## 定义",
      "一句话说明这个指标衡量什么。",
      "",
      "## 计算公式",
      "例如：{名称} = 分子 / 分母。写清分子、分母各自的取数逻辑。",
      "",
      "## 统计粒度",
      "按天 / 周 / 月；按门店 / 区域 / 产品线……",
      "",
      "## 时间口径",
      "以下单时间 / 支付时间 / 发货时间为准；时区；是否含当天。",
      "",
      "## 包含和排除",
      "- 包含：",
      "- 排除：测试订单、内部订单、已取消订单……",
      "",
      "## 数据来源",
      "表名（catalog.schema.table）和关键字段。",
      "",
      "## 负责人",
      "业务负责人：",
      "数据负责人：",
      "",
      "## 生效日期",
      "从 YYYY-MM-DD 起按本口径统计。",
      "",
    ].join("\n"),
  },
  {
    id: "table",
    title: "表说明",
    filename: "表说明-{名称}.md",
    body: [
      "# {名称}",
      "",
      "## 用途",
      "这张表回答什么问题，谁在用。",
      "",
      "## 粒度",
      "一行代表什么（例如：一个订单的一个商品）。",
      "",
      "## 主键",
      "字段名；是否全局唯一。",
      "",
      "## 字段说明",
      "| 字段 | 类型 | 含义 | 取值或示例 |",
      "| --- | --- | --- | --- |",
      "|  |  |  |  |",
      "",
      "## 更新频率",
      "每天几点更新；全量还是增量；延迟多久。",
      "",
      "## 上游",
      "来源系统或上游表；生成这张表的作业。",
      "",
    ].join("\n"),
  },
  {
    id: "rule",
    title: "业务规则",
    filename: "业务规则-{名称}.md",
    body: [
      "# {名称}",
      "",
      "## 规则",
      "用一两句话写清规则本身。",
      "",
      "## 适用范围",
      "适用于哪些业务、地区、客户或产品。",
      "",
      "## 例外",
      "哪些情况不适用，怎么处理。",
      "",
      "## 依据",
      "制度文件、会议纪要或审批单号。",
      "",
      "## 生效日期",
      "从 YYYY-MM-DD 起执行。",
      "",
    ].join("\n"),
  },
];
