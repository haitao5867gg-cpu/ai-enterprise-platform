"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import {
  deleteDocument,
  deleteKnowledgeBase,
  documentFileUrl,
  documentStatusLabels,
  getKnowledgeBase,
  getKnowledgeOptions,
  listDocuments,
  reindexDocument,
  reindexKnowledgeBase,
  searchKnowledgeBase,
  updateKnowledgeBase,
  uploadDocuments,
  uploadProblem,
  type Document,
  type Hit,
  type KnowledgeBase,
  type KnowledgeOptions,
  type Retrieval,
} from "../lib/knowledge";
import { retrievalProblems } from "../lib/retrieval";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { ErrorNotice } from "./ui";
import { KnowledgeForm } from "./knowledge-form";
import { KnowledgeChunks } from "./knowledge-chunks";
import { RetrievalFields } from "./retrieval-fields";

type Selection = { documentId: string; name: string; position?: number };
function SearchTest({
  kb,
  open,
  options,
}: {
  kb: string;
  open: (selection: Selection) => void;
  options: KnowledgeOptions;
}) {
  const queryId = useId();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [minScore, setMinScore] = useState(0.3);
  const [retrieval, setRetrieval] = useState<Retrieval>(options.retrieval);
  const retrievalErrors = Object.values(
    retrievalProblems(retrieval, topK, options.rerank_models),
  );
  const [result, setResult] = useState<{
    hits: Hit[];
    duration_ms: number;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <section className="panel knowledge-section" aria-label="检索测试">
      <h2>检索测试</h2>
      <p className="muted">
        检查问题能命中哪些资料，调整检索方式、片段数与最低分数。
      </p>
      <form
        className="asset-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (retrievalErrors.length) return;
          setBusy(true);
          setError("");
          setResult(null);
          try {
            setResult(
              await searchKnowledgeBase(kb, {
                query,
                top_k: topK,
                min_score: minScore,
                retrieval,
              }),
            );
          } catch (err) {
            setError(errorMessage(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="asset-field">
          <label htmlFor={queryId}>问题</label>
          <textarea
            id={queryId}
            required
            maxLength={2000}
            rows={3}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：退换货的期限是多久？"
          />
        </div>
        <div className="knowledge-grid">
          <label className="asset-field">
            top_k
            <input
              type="number"
              required
              min={1}
              max={20}
              step={1}
              value={Number.isNaN(topK) ? "" : topK}
              onChange={(event) => setTopK(event.target.valueAsNumber)}
            />
          </label>
          <label className="asset-field">
            min_score
            <input
              type="number"
              required
              min={0}
              max={1}
              step="any"
              value={Number.isNaN(minScore) ? "" : minScore}
              onChange={(event) => setMinScore(event.target.valueAsNumber)}
            />
          </label>
        </div>
        <RetrievalFields
          value={retrieval}
          topK={topK}
          models={options.rerank_models}
          onChange={setRetrieval}
        />
        <button
          className="button primary"
          disabled={busy || !query.trim() || retrievalErrors.length > 0}
        >
          {busy ? "正在检索…" : "检索"}
        </button>
      </form>
      <div
        className="knowledge-search-results"
        role="region"
        aria-label="检索结果"
        aria-live="polite"
        aria-busy={busy}
      >
        {error && <ErrorNotice message={error} />}
        {result && (
          <>
            <p className="muted">
              {result.hits.length} 条命中 · 耗时 {result.duration_ms} ms
            </p>
            {!result.hits.length && (
              <p>没有符合条件的片段，请调整问题或最低分数。</p>
            )}
            {result.hits.map((hit) => (
              <article className="knowledge-chunk" key={hit.chunk_id}>
                <div className="asset-section-heading">
                  <button
                    className="text-button"
                    onClick={() =>
                      open({
                        documentId: hit.document_id,
                        name: hit.document_name,
                        position: hit.position,
                      })
                    }
                  >
                    {hit.document_name}
                  </button>
                  <strong className="knowledge-score">
                    最终分数 {hit.score.toFixed(2)}
                  </strong>
                </div>
                <div className="knowledge-scores" aria-label="分数明细">
                  {(
                    [
                      ["vector", "向量"],
                      ["keyword", "关键词"],
                      ["rerank", "重排"],
                    ] as const
                  ).map(
                    ([key, label]) =>
                      hit.scores?.[key] != null && (
                        <span key={key}>
                          {label} {hit.scores[key].toFixed(2)}
                        </span>
                      ),
                  )}
                </div>
                <p className="muted">
                  {hit.heading || "无标题路径"}
                  {hit.page !== null && ` · 第 ${hit.page} 页`}
                </p>
                <p className="knowledge-text">{hit.text}</p>
              </article>
            ))}
          </>
        )}
      </div>
    </section>
  );
}

export function KnowledgeDetail({ id }: { id: string }) {
  const router = useRouter();
  const [base, setBase] = useState<KnowledgeBase | null>(null);
  const [options, setOptions] = useState<KnowledgeOptions | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [tab, setTab] = useState("documents");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const hasPending = documents.some(
    (doc) => doc.status === "queued" || doc.status === "processing",
  );
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      listDocuments(id, controller.signal),
      getKnowledgeOptions(controller.signal),
    ])
      .then(async ([docs, opts]) => {
        const kb = await getKnowledgeBase(id, controller.signal);
        if (!controller.signal.aborted) {
          setBase(kb);
          setOptions(opts);
          setDocuments(docs);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [id, revision]);
  useEffect(() => {
    if (!hasPending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const docs = await listDocuments(id, controller.signal);
        const kb = await getKnowledgeBase(id, controller.signal);
        if (controller.signal.aborted) return;
        setDocuments(docs);
        setBase(kb);
        setError("");
        if (docs.some((doc) => ["queued", "processing"].includes(doc.status)))
          timer = setTimeout(poll, 3000);
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(errorMessage(err));
          timer = setTimeout(poll, 3000);
        }
      }
    }
    timer = setTimeout(poll, 3000);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, hasPending]);
  async function refresh() {
    const docs = await listDocuments(id);
    const kb = await getKnowledgeBase(id);
    setDocuments(docs);
    setBase(kb);
  }
  async function mutate(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  function upload(files: File[]) {
    const problem = uploadProblem(files);
    if (problem) {
      setError(problem);
      return;
    }
    void mutate(() => uploadDocuments(id, files));
  }
  if (!base || !options)
    return error ? (
      <ErrorNotice message={error} onRetry={() => setRevision((n) => n + 1)} />
    ) : (
      <AssetLoading />
    );
  return (
    <>
      <Link className="back-link" href="/knowledge">
        ← 知识库
      </Link>
      <AssetHeading
        eyebrow="KNOWLEDGE BASE"
        title={base.name}
        action={<span className="app-tag">{base.embedding_model}</span>}
      >
        {base.description || "管理文档、测试检索并配置切分规则。"}
      </AssetHeading>
      <div className="knowledge-overview">
        <span>{base.document_count} 个文档</span>
        <span>{base.chunk_count} 个片段</span>
        <span>
          {base.status_counts.queued + base.status_counts.processing} 个处理中
        </span>
      </div>
      <div
        className="filter-tabs knowledge-tabs"
        role="tablist"
        aria-label="知识库详情"
      >
        {[
          ["documents", "文档"],
          ["search", "检索测试"],
          ["settings", "设置"],
        ].map(([value, label]) => (
          <button
            role="tab"
            id={`tab-${value}`}
            aria-controls={`panel-${value}`}
            aria-selected={tab === value}
            className={tab === value ? "selected" : ""}
            key={value}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "documents" && (
          <section className="panel knowledge-section">
            <div className="asset-section-heading">
              <h2>文档</h2>
              <button
                className="button secondary"
                disabled={busy || !documents.length}
                onClick={() => {
                  if (
                    window.confirm(
                      "确定全部重建？所有文档将按当前切分设置重新处理。",
                    )
                  )
                    void mutate(() => reindexKnowledgeBase(id));
                }}
              >
                全部重建
              </button>
            </div>
            <div
              className={`knowledge-dropzone ${dragging ? "dragging" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (!busy) upload(Array.from(event.dataTransfer.files));
              }}
            >
              <input
                ref={fileInput}
                className="sr-only"
                id="knowledge-upload"
                aria-label="上传文档"
                type="file"
                multiple
                accept=".pdf,.docx,.md,.txt"
                disabled={busy}
                onChange={(event) => {
                  upload(Array.from(event.target.files ?? []));
                  event.target.value = "";
                }}
              />
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => fileInput.current?.click()}
              >
                {busy ? "正在提交…" : "点击上传文档"}
              </button>
              <p>或将文件拖拽到这里</p>
              <small className="muted">
                PDF / DOCX / MD / TXT · 每个文件最多 20 MB · 每次最多 20 个 ·
                每库最多 500 个
              </small>
            </div>
            {hasPending && (
              <p className="muted" role="status">
                文档处理中，每 3 秒自动刷新；全部处理完成后停止。
              </p>
            )}
            <div className="table-scroll">
              <table
                className="mapping-table knowledge-table"
                aria-label="文档列表"
              >
                <thead>
                  <tr>
                    {[
                      "名称",
                      "类型",
                      "大小",
                      "状态",
                      "片段数",
                      "上传时间",
                      "操作",
                    ].map((label) => (
                      <th scope="col" key={label}>
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {documents.map((doc) => (
                    <tr key={doc.document_id}>
                      <td>{doc.name}</td>
                      <td>{doc.kind.toUpperCase()}</td>
                      <td>
                        {doc.size_bytes >= 1024 * 1024
                          ? `${(doc.size_bytes / 1024 / 1024).toFixed(1)} MB`
                          : `${(doc.size_bytes / 1024).toFixed(1)} KB`}
                      </td>
                      <td>
                        <span className={`knowledge-status ${doc.status}`}>
                          {documentStatusLabels[doc.status]}
                        </span>
                        {doc.error && (
                          <p className="field-error">{doc.error}</p>
                        )}
                      </td>
                      <td>{doc.chunk_count}</td>
                      <td>
                        <AssetTime value={doc.created_at} />
                      </td>
                      <td>
                        <div className="knowledge-doc-actions">
                          <button
                            className="text-button"
                            disabled={doc.status !== "ready"}
                            onClick={() =>
                              setSelection({
                                documentId: doc.document_id,
                                name: doc.name,
                              })
                            }
                          >
                            查看片段
                          </button>
                          <a
                            className="text-button"
                            href={documentFileUrl(id, doc.document_id)}
                            download
                          >
                            下载原文件
                          </a>
                          <button
                            className="text-button"
                            disabled={
                              busy ||
                              ["queued", "processing"].includes(doc.status)
                            }
                            onClick={() =>
                              void mutate(() =>
                                reindexDocument(id, doc.document_id),
                              )
                            }
                          >
                            重建
                          </button>
                          <button
                            className="text-button danger-text"
                            disabled={busy}
                            onClick={() => {
                              if (
                                window.confirm(
                                  `确定删除「${doc.name}」及其全部片段？`,
                                )
                              )
                                void mutate(() =>
                                  deleteDocument(id, doc.document_id),
                                );
                            }}
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {documents.length === 0 && (
              <p className="knowledge-empty muted">
                还没有文档，上传资料后即可检索。
              </p>
            )}
          </section>
        )}
        {tab === "search" && (
          <SearchTest kb={id} options={options} open={setSelection} />
        )}
        {tab === "settings" && (
          <section className="panel knowledge-section" aria-label="知识库设置">
            <h2>知识库设置</h2>
            <KnowledgeForm
              initial={base}
              options={options}
              save={async (input) => {
                setBase(await updateKnowledgeBase(id, input));
              }}
            />
            <div className="knowledge-danger">
              <h3>删除知识库</h3>
              <p className="muted">删除后，文档、片段和原文件将一并移除。</p>
              <button
                className="button secondary danger-text"
                disabled={busy}
                onClick={async () => {
                  if (
                    !window.confirm(
                      `确定删除知识库「${base.name}」及其全部文档？`,
                    )
                  )
                    return;
                  setBusy(true);
                  setError("");
                  try {
                    await deleteKnowledgeBase(id);
                    router.push("/knowledge");
                  } catch (err) {
                    setError(errorMessage(err));
                    setBusy(false);
                  }
                }}
              >
                删除知识库
              </button>
            </div>
          </section>
        )}
      </div>
      {selection && (
        <KnowledgeChunks
          key={`${selection.documentId}-${selection.position}`}
          kb={id}
          {...selection}
          close={() => setSelection(null)}
        />
      )}
    </>
  );
}
