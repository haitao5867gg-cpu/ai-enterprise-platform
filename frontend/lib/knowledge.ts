import { requestJson } from "./runs";

// docs/api/knowledge.md is the source of truth for these wire types.
export type Chunking = { size: number; overlap: number };
export type Retrieval = {
  mode: "hybrid" | "vector" | "keyword";
  vector_weight: number;
  rerank: { model: string; candidates: number } | null;
};
export type RerankModel = { model: string; kind: "rerank" | "llm" };
export type DocumentStatus = "queued" | "processing" | "ready" | "error";
export type KnowledgeBase = {
  kb_id: string;
  name: string;
  description: string;
  embedding_model: string;
  chunking: Chunking;
  document_count: number;
  chunk_count: number;
  status_counts: Record<DocumentStatus, number>;
  validity_counts: Record<Validity, number>;
  created_at: string;
  updated_at: string;
};
// docs/api/knowledge-governance.md
export type Validity = "active" | "pending" | "expired";
export type Document = {
  document_id: string;
  kb_id: string;
  name: string;
  kind: "pdf" | "docx" | "md" | "txt";
  size_bytes: number;
  status: DocumentStatus;
  error: string | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
  owner: string | null;
  valid_from: string | null;
  valid_until: string | null;
  validity: Validity;
  version: number;
  uploaded_by: string | null;
  hits_30d: number;
  last_hit_at: string | null;
};
export type DocumentVersion = {
  version: number;
  size_bytes: number;
  uploaded_by: string | null;
  uploaded_at: string;
};
export type DocumentChanges = Partial<
  Pick<Document, "owner" | "valid_from" | "valid_until">
>;
export type DocTemplate = {
  id: string;
  title: string;
  filename: string;
  body: string;
};
export type RegressionCase = { id: string; query: string; expected: string[] };
export type RegressionResult = {
  id: string;
  passed: boolean;
  found: string[];
  missing: string[];
  top: string[];
  error: string | null;
};
export type Regression = {
  kb_id: string;
  cases: RegressionCase[];
  retrieval: Retrieval;
  top_k: number;
  min_score: number;
  updated_by: string | null;
  updated_at: string | null;
  last_run: {
    at: string;
    by: string | null;
    passed: number;
    total: number;
    score: number | null;
    results: RegressionResult[];
  } | null;
  stale: boolean;
};
export const MAX_CASES = 50;
export type Chunk = {
  chunk_id: string;
  document_id: string;
  position: number;
  text: string;
  heading: string;
  page: number | null;
};
export type Hit = Omit<Chunk, "document_id"> & {
  kb_id: string;
  kb_name: string;
  document_id: string;
  document_name: string;
  score: number;
  scores: {
    vector: number | null;
    keyword: number | null;
    rerank: number | null;
  };
};
export type Reference = Hit & { index: number };
// `cited`: whether what the run shows refers to it by [n]; missing in lists
// that are all cited.
export type RunSource = Reference & {
  node: string;
  node_title: string;
  cited?: boolean;
};
export type KnowledgeOption = Pick<
  KnowledgeBase,
  "kb_id" | "name" | "document_count"
>;
export type KnowledgeOptions = {
  embedding_models: string[];
  default_embedding_model: string;
  chunking: Chunking;
  retrieval: Retrieval;
  rerank_models: RerankModel[];
};
export type KnowledgeInput = Pick<KnowledgeBase, "name"> &
  Partial<Pick<KnowledgeBase, "description" | "embedding_model" | "chunking">>;
const base = (kb: string) => `/knowledge-bases/${encodeURIComponent(kb)}`;
const doc = (kb: string, id: string) =>
  `${base(kb)}/documents/${encodeURIComponent(id)}`;
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const getKnowledgeOptions = (signal?: AbortSignal) =>
  requestJson("/knowledge-bases/options", {
    signal,
  }) as Promise<KnowledgeOptions>;
export const listKnowledgeBases = (signal?: AbortSignal) =>
  requestJson("/knowledge-bases", { signal }) as Promise<KnowledgeBase[]>;
export const createKnowledgeBase = (input: KnowledgeInput) =>
  requestJson(
    "/knowledge-bases",
    json("POST", input),
  ) as Promise<KnowledgeBase>;
export const getKnowledgeBase = (kb: string, signal?: AbortSignal) =>
  requestJson(base(kb), { signal }) as Promise<KnowledgeBase>;
export const updateKnowledgeBase = (
  kb: string,
  input: Partial<KnowledgeInput>,
) => requestJson(base(kb), json("PATCH", input)) as Promise<KnowledgeBase>;
export const deleteKnowledgeBase = (kb: string) =>
  requestJson(base(kb), json("DELETE"));
export const listDocuments = (kb: string, signal?: AbortSignal) =>
  requestJson(`${base(kb)}/documents`, { signal }) as Promise<Document[]>;
export const uploadDocuments = (
  kb: string,
  files: File[],
  options: { replace?: boolean; uploadedBy?: string } = {},
) => {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  if (options.replace) body.append("replace", "true");
  if (options.uploadedBy) body.append("uploaded_by", options.uploadedBy);
  return requestJson(`${base(kb)}/documents`, {
    method: "POST",
    body,
  }) as Promise<Document[]>;
};
export const getDocument = (kb: string, id: string, signal?: AbortSignal) =>
  requestJson(doc(kb, id), { signal }) as Promise<Document>;
export const updateDocument = (
  kb: string,
  id: string,
  changes: DocumentChanges,
) => requestJson(doc(kb, id), json("PATCH", changes)) as Promise<Document>;
export const listDocumentVersions = (kb: string, id: string) =>
  requestJson(`${doc(kb, id)}/versions`) as Promise<DocumentVersion[]>;
export const listDocTemplates = (signal?: AbortSignal) =>
  requestJson("/knowledge-bases/doc-templates", { signal }) as Promise<
    DocTemplate[]
  >;
export const getRegression = (kb: string, signal?: AbortSignal) =>
  requestJson(`${base(kb)}/regression`, { signal }) as Promise<Regression>;
export const saveRegression = (
  kb: string,
  input: Pick<Regression, "cases" | "retrieval" | "top_k" | "min_score"> & {
    actor?: string;
  },
) =>
  requestJson(
    `${base(kb)}/regression`,
    json("PUT", input),
  ) as Promise<Regression>;
export const runRegression = (kb: string, actor?: string) =>
  requestJson(
    `${base(kb)}/regression/run`,
    json("POST", actor ? { actor } : {}),
  ) as Promise<Regression>;
export const deleteDocument = (kb: string, id: string) =>
  requestJson(doc(kb, id), json("DELETE"));
export const documentFileUrl = (kb: string, id: string) =>
  `/api${doc(kb, id)}/file`;
export const getChunks = (
  kb: string,
  id: string,
  offset = 0,
  signal?: AbortSignal,
) =>
  requestJson(`${doc(kb, id)}/chunks?offset=${offset}&limit=50`, {
    signal,
  }) as Promise<{ total: number; items: Chunk[] }>;
export const reindexDocument = (kb: string, id: string) =>
  requestJson(`${doc(kb, id)}/reindex`, json("POST")) as Promise<Document>;
export const reindexKnowledgeBase = (kb: string) =>
  requestJson(`${base(kb)}/reindex`, json("POST")) as Promise<{
    queued: number;
  }>;
export const searchKnowledgeBase = (
  kb: string,
  input: {
    query: string;
    top_k?: number;
    min_score?: number;
    retrieval?: Retrieval;
    include_inactive?: boolean;
  },
) =>
  requestJson(`${base(kb)}/search`, json("POST", input)) as Promise<{
    hits: Hit[];
    duration_ms: number;
  }>;

export const documentStatusLabels: Record<DocumentStatus, string> = {
  queued: "排队中",
  processing: "处理中",
  ready: "可用",
  error: "失败",
};
export function uploadProblem(files: Pick<File, "name" | "size">[]): string {
  if (!files.length) return "请选择文件";
  if (files.length > 20) return "一次最多上传 20 个文件";
  for (const file of files) {
    if (!/\.(pdf|docx|md|txt)$/i.test(file.name))
      return `${file.name}：仅支持 pdf、docx、md、txt 文件`;
    if (file.size > 20 * 1024 * 1024) return `${file.name}：每个文件最多 20 MB`;
  }
  return "";
}

export const validityLabels: Record<Validity, string> = {
  active: "有效",
  pending: "未生效",
  expired: "已失效",
};
// "长期有效", "2026-01-01 起", "到 2026-12-31" or "2026-01-01 至 2026-12-31".
export function validityRange(
  doc: Pick<Document, "valid_from" | "valid_until">,
): string {
  if (doc.valid_from && doc.valid_until)
    return `${doc.valid_from} 至 ${doc.valid_until}`;
  if (doc.valid_from) return `${doc.valid_from} 起`;
  if (doc.valid_until) return `到 ${doc.valid_until}`;
  return "长期有效";
}
