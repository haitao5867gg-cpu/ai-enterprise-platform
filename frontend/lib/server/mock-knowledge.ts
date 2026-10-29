import "server-only";
import type {
  Chunk,
  Document,
  Hit,
  KnowledgeBase,
  KnowledgeOptions,
  Retrieval,
} from "../knowledge";
import { uploadProblem } from "../knowledge";
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
type StoredDocument = {
  document: Document;
  chunks: Chunk[];
  bytes: Uint8Array;
  text: string;
  polls: number;
};
type StoredKnowledge = {
  base: KnowledgeBase;
  documents: Map<string, StoredDocument>;
};
const now = () => new Date().toISOString();
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
          },
          chunks,
          text: sampleText,
          bytes: new TextEncoder().encode("%PDF-1.4\nMock policy document\n"),
          polls: 3,
        },
      ],
    ]),
  });
}
function snapshot(entry: StoredKnowledge): KnowledgeBase {
  const documents = [...entry.documents.values()].map((item) => item.document);
  return {
    ...entry.base,
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
        .filter((item) => item.document.status === "ready")
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
    const base: KnowledgeBase = {
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
    store.set(kb_id, { base, documents: new Map() });
    return mockJson(base, 201);
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
    const { query, top_k = 5, min_score = 0.3, retrieval } = body;
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
  if (!docId) {
    if (method === "GET") {
      tick(entry);
      return mockJson(
        [...entry.documents.values()].map((item) => item.document).reverse(),
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
    if (entry.documents.size + files.length > 500)
      return mockError("一个知识库最多 500 个文档", 422);
    const result: Document[] = [];
    for (const file of files) {
      const document_id = crypto.randomUUID();
      const kind = file.name
        .split(".")
        .at(-1)!
        .toLowerCase() as Document["kind"];
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
      const document: Document = {
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
      };
      entry.documents.set(document_id, {
        document,
        chunks: [],
        bytes,
        text,
        polls: 0,
      });
      result.push(document);
    }
    return mockJson(result, 202);
  }
  const item = entry.documents.get(docId);
  if (!item) return mockError("没有这个文档", 404);
  if (!action) {
    if (method === "DELETE") {
      entry.documents.delete(docId);
      return noContent();
    }
    return mockJson(item.document);
  }
  if (action === "reindex") {
    queue(item);
    return mockJson(item.document, 202);
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
