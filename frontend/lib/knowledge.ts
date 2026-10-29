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
  created_at: string;
  updated_at: string;
};
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
};
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
export type RunSource = Reference & { node: string; node_title: string };
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
export const uploadDocuments = (kb: string, files: File[]) => {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  return requestJson(`${base(kb)}/documents`, {
    method: "POST",
    body,
  }) as Promise<Document[]>;
};
export const getDocument = (kb: string, id: string, signal?: AbortSignal) =>
  requestJson(doc(kb, id), { signal }) as Promise<Document>;
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
