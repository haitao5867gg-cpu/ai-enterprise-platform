"use client";

import { useEffect, useState } from "react";
import {
  listDocTemplates,
  listDocumentVersions,
  MAX_CASES,
  runRegression,
  saveRegression,
  updateDocument,
  uploadDocuments,
  type DocTemplate,
  type Document,
  type DocumentVersion,
  type KnowledgeOptions,
  type Regression,
  type RegressionCase,
} from "../lib/knowledge";
import { retrievalProblems } from "../lib/retrieval";
import { errorMessage } from "../lib/runs";
import { AssetTime } from "./asset-ui";
import { Dialog, useActor } from "./catalog-list";
import { RetrievalFields } from "./retrieval-fields";
import { ErrorNotice } from "./ui";

// docs/api/knowledge-governance.md: owners, validity dates, versions, templates and
// retrieval regression on the knowledge base page.

export function EditDocumentDialog({
  kb,
  doc,
  close,
  saved,
}: {
  kb: string;
  doc: Document;
  close: () => void;
  saved: (doc: Document) => void;
}) {
  const [owner, setOwner] = useState(doc.owner ?? "");
  const [from, setFrom] = useState(doc.valid_from ?? "");
  const [until, setUntil] = useState(doc.valid_until ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    if (from && until && until < from)
      return setError("失效日期不能早于生效日期");
    setBusy(true);
    setError("");
    try {
      saved(
        await updateDocument(kb, doc.document_id, {
          owner: owner.trim() || null,
          valid_from: from || null,
          valid_until: until || null,
        }),
      );
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <Dialog title={`编辑「${doc.name}」`} close={close}>
      <form
        className="asset-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {error && <ErrorNotice message={error} />}
        <label className="asset-field">
          负责人
          <input
            maxLength={50}
            value={owner}
            disabled={busy}
            onChange={(event) => setOwner(event.target.value)}
          />
        </label>
        <div className="knowledge-grid">
          <label className="asset-field">
            生效日期
            <input
              type="date"
              value={from}
              disabled={busy}
              onChange={(event) => setFrom(event.target.value)}
            />
          </label>
          <label className="asset-field">
            失效日期
            <input
              type="date"
              value={until}
              disabled={busy}
              onChange={(event) => setUntil(event.target.value)}
            />
          </label>
        </div>
        <small className="muted">
          日期都含当天；不填表示一直有效。没生效和已失效的文档不会被检索到。
        </small>
        <div className="asset-actions">
          <button className="button primary" disabled={busy}>
            {busy ? "正在保存…" : "保存"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function VersionsDialog({
  kb,
  doc,
  close,
}: {
  kb: string;
  doc: Document;
  close: () => void;
}) {
  const [versions, setVersions] = useState<DocumentVersion[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    listDocumentVersions(kb, doc.document_id)
      .then(setVersions)
      .catch((err) => setError(errorMessage(err)));
  }, [kb, doc.document_id]);
  return (
    <Dialog title={`「${doc.name}」的版本记录`} close={close}>
      {error && <ErrorNotice message={error} />}
      {!versions && !error && <p className="muted">正在加载…</p>}
      {versions && (
        <table className="mapping-table" aria-label="版本记录">
          <thead>
            <tr>
              <th scope="col">版本</th>
              <th scope="col">大小</th>
              <th scope="col">上传的人</th>
              <th scope="col">时间</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td>v{v.version}</td>
                <td>{(v.size_bytes / 1024).toFixed(1)} KB</td>
                <td>{v.uploaded_by ?? "—"}</td>
                <td>
                  <AssetTime value={v.uploaded_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted">
        只留当前版本的原文件；要留底，可以在资产目录把知识库提交到资产仓库。
      </p>
    </Dialog>
  );
}

const fill = (text: string, name: string) => text.replaceAll("{名称}", name);

export function TemplateDialog({
  kb,
  close,
  saved,
}: {
  kb: string;
  close: () => void;
  saved: () => void;
}) {
  const actor = useActor();
  const [templates, setTemplates] = useState<DocTemplate[] | null>(null);
  const [chosen, setChosen] = useState("");
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    listDocTemplates()
      .then((found) => {
        setTemplates(found);
        if (found[0]) {
          setChosen(found[0].id);
          setBody(found[0].body);
        }
      })
      .catch((err) => setError(errorMessage(err)));
  }, []);
  const template = templates?.find((t) => t.id === chosen);
  const filename =
    template && name.trim() ? fill(template.filename, name.trim()) : "";
  function pick(id: string) {
    const next = templates?.find((t) => t.id === id);
    if (!next) return;
    if (edited && !window.confirm("换模板会丢掉已经改的正文，确定吗？")) return;
    setChosen(id);
    setBody(fill(next.body, name.trim() || "{名称}"));
    setEdited(false);
  }
  async function save() {
    if (!filename) return setError("请填名称");
    if (/[\\/]/.test(name)) return setError("名称里不能有 / 或 \\");
    setBusy(true);
    setError("");
    try {
      const file = new File([fill(body, name.trim())], filename, {
        type: "text/markdown",
      });
      await uploadDocuments(kb, [file], { uploadedBy: actor() });
      saved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <Dialog title="用模板写文档" close={close}>
      <form
        className="asset-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {error && <ErrorNotice message={error} />}
        {templates && (
          <fieldset className="asset-field">
            <legend>模板</legend>
            <div className="filter-tabs">
              {templates.map((t) => (
                <button
                  type="button"
                  key={t.id}
                  aria-pressed={t.id === chosen}
                  className={t.id === chosen ? "selected" : ""}
                  onClick={() => pick(t.id)}
                >
                  {t.title}
                </button>
              ))}
            </div>
          </fieldset>
        )}
        <label className="asset-field">
          名称
          <input
            required
            maxLength={60}
            value={name}
            disabled={busy}
            placeholder="例如：有效订单数"
            onChange={(event) => {
              setName(event.target.value);
              if (!edited && template)
                setBody(
                  fill(template.body, event.target.value.trim() || "{名称}"),
                );
            }}
          />
          {filename && <small className="muted">保存为 {filename}</small>}
        </label>
        <label className="asset-field">
          正文
          <textarea
            rows={16}
            value={body}
            disabled={busy}
            onChange={(event) => {
              setBody(event.target.value);
              setEdited(true);
            }}
          />
        </label>
        <div className="asset-actions">
          <button className="button primary" disabled={busy || !templates}>
            {busy ? "正在保存…" : "保存到知识库"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

let nextCase = 0;
type DraftCase = RegressionCase & { key: number };
const draft = (item: RegressionCase): DraftCase => ({
  ...item,
  key: nextCase++,
});

function newCaseId(cases: RegressionCase[]) {
  let n = cases.length + 1;
  while (cases.some((item) => item.id === `c${n}`)) n++;
  return `c${n}`;
}

export function RegressionPanel({
  kb,
  documents,
  options,
  initial,
  changed,
}: {
  kb: string;
  documents: Document[];
  options: KnowledgeOptions;
  initial: Regression;
  changed: (regression: Regression) => void;
}) {
  const actor = useActor();
  const [regression, setRegression] = useState(initial);
  const [cases, setCases] = useState<DraftCase[]>(() =>
    initial.cases.map(draft),
  );
  const [topK, setTopK] = useState(initial.top_k);
  const [minScore, setMinScore] = useState(initial.min_score);
  const [retrieval, setRetrieval] = useState(initial.retrieval);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "run">("");
  const [error, setError] = useState("");
  const names = documents.map((doc) => doc.name);
  const problems = [
    ...Object.values(retrievalProblems(retrieval, topK, options.rerank_models)),
    ...cases.flatMap((item, i) =>
      [
        !item.query.trim() && `第 ${i + 1} 条没填问题`,
        !item.expected.length && `第 ${i + 1} 条没选应找到的文档`,
        item.expected.length > 5 && `第 ${i + 1} 条最多选 5 个文档`,
      ].filter((text): text is string => Boolean(text)),
    ),
  ];
  function edit(next: DraftCase[]) {
    setCases(next);
    setDirty(true);
  }
  function update(key: number, change: Partial<RegressionCase>) {
    edit(
      cases.map((item) => (item.key === key ? { ...item, ...change } : item)),
    );
  }
  function done(next: Regression) {
    setRegression(next);
    changed(next);
  }
  async function save() {
    setBusy("save");
    setError("");
    try {
      const next = await saveRegression(kb, {
        cases: cases.map(({ id, query, expected }) => ({
          id,
          query,
          expected,
        })),
        retrieval,
        top_k: topK,
        min_score: minScore,
        actor: actor(),
      });
      done(next);
      setDirty(false);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy("");
    }
  }
  async function run() {
    if (dirty && !(await save())) return;
    setBusy("run");
    setError("");
    try {
      done(await runRegression(kb, actor()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }
  const last = regression.last_run;
  const byId = new Map(last?.results.map((result) => [result.id, result]));
  return (
    <section className="panel knowledge-section" aria-label="检索回归">
      <h2>检索回归</h2>
      <p className="muted">
        写下常问的问题和应该找到的文档；改了文档后跑一遍，看该找到的还找不找得到。
      </p>
      {regression.stale && !dirty && (
        <p className="asset-notice" role="status">
          {last ? "文档改过，建议重跑回归。" : "用例还没跑过。"}
        </p>
      )}
      {error && <ErrorNotice message={error} />}
      {last && (
        <p className="knowledge-overview" aria-label="回归结果">
          <span>
            通过 {last.passed} / {last.total}
            {last.score !== null && `（${Math.round(last.score * 100)}%）`}
          </span>
          <span>
            <AssetTime value={last.at} />
            {last.by && ` · ${last.by}`}
          </span>
        </p>
      )}
      <div className="table-scroll">
        <table className="mapping-table" aria-label="检索用例">
          <thead>
            <tr>
              <th scope="col">问题</th>
              <th scope="col">应找到的文档</th>
              <th scope="col">上次结果</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((item, i) => {
              const result = !dirty ? byId.get(item.id) : undefined;
              return (
                <tr key={item.key}>
                  <td>
                    <textarea
                      aria-label={`第 ${i + 1} 条的问题`}
                      rows={2}
                      maxLength={500}
                      value={item.query}
                      onChange={(event) =>
                        update(item.key, { query: event.target.value })
                      }
                    />
                  </td>
                  <td>
                    <select
                      multiple
                      aria-label={`第 ${i + 1} 条应找到的文档`}
                      value={item.expected}
                      onChange={(event) =>
                        update(item.key, {
                          expected: Array.from(
                            event.target.selectedOptions,
                            (option) => option.value,
                          ),
                        })
                      }
                    >
                      {[...new Set([...names, ...item.expected])].map(
                        (name) => (
                          <option key={name} value={name}>
                            {names.includes(name) ? name : `${name}（已删除）`}
                          </option>
                        ),
                      )}
                    </select>
                  </td>
                  <td aria-label={`第 ${i + 1} 条的结果`}>
                    {result ? (
                      <>
                        <span
                          className={`knowledge-status ${result.passed ? "ready" : "error"}`}
                        >
                          {result.passed ? "通过" : "没通过"}
                        </span>
                        {result.error && (
                          <p className="field-error">{result.error}</p>
                        )}
                        {result.missing.length > 0 && (
                          <p className="muted">
                            缺：{result.missing.join("、")}
                          </p>
                        )}
                        <p className="muted">
                          找到：{result.top.join("、") || "没有命中"}
                        </p>
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="text-button danger-text"
                      onClick={() =>
                        edit(cases.filter((other) => other.key !== item.key))
                      }
                    >
                      删除
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!cases.length && (
        <p className="knowledge-empty muted">还没有用例，先加一条。</p>
      )}
      <button
        type="button"
        className="button secondary"
        disabled={cases.length >= MAX_CASES || !names.length}
        onClick={() =>
          edit([
            ...cases,
            draft({ id: newCaseId(cases), query: "", expected: [] }),
          ])
        }
      >
        加一条用例
      </button>
      <h3>检索设置</h3>
      <div className="knowledge-grid">
        <label className="asset-field">
          top_k
          <input
            type="number"
            min={1}
            max={20}
            value={Number.isNaN(topK) ? "" : topK}
            onChange={(event) => {
              setTopK(event.target.valueAsNumber);
              setDirty(true);
            }}
          />
        </label>
        <label className="asset-field">
          min_score
          <input
            type="number"
            min={0}
            max={1}
            step="any"
            value={Number.isNaN(minScore) ? "" : minScore}
            onChange={(event) => {
              setMinScore(event.target.valueAsNumber);
              setDirty(true);
            }}
          />
        </label>
      </div>
      <RetrievalFields
        value={retrieval}
        topK={topK}
        models={options.rerank_models}
        onChange={(next) => {
          setRetrieval(next);
          setDirty(true);
        }}
      />
      {problems.length > 0 && (
        <ul className="field-error">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      <div className="knowledge-doc-actions">
        <button
          type="button"
          className="button secondary"
          disabled={Boolean(busy) || !dirty || problems.length > 0}
          onClick={() => void save()}
        >
          {busy === "save" ? "正在保存…" : "保存"}
        </button>
        <button
          type="button"
          className="button primary"
          disabled={Boolean(busy) || !cases.length || problems.length > 0}
          onClick={() => void run()}
        >
          {busy === "run" ? "正在运行…" : "运行回归"}
        </button>
      </div>
    </section>
  );
}
