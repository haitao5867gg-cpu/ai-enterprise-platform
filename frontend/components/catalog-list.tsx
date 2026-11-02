"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { savedActor } from "../lib/actor";
import {
  catalogKinds,
  kindLabel,
  listCatalog,
  MAX_TAG,
  MAX_TAGS,
  parseTags,
  percent,
  proposeAsset,
  setCatalogMeta,
  type CatalogItem,
  type CatalogKind,
  type CatalogList,
  type Proposed,
} from "../lib/catalog";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { Icon } from "./icons";
import { useSignedIn } from "./session";
import { EmptyState, ErrorNotice } from "./ui";

// docs/api/catalog.md: every platform asset in one list.

const key = (item: Pick<CatalogItem, "kind" | "id">) =>
  `${item.kind}:${item.id}`;

function matches(item: CatalogItem, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [item.title, item.summary, item.owner ?? "", ...item.tags].some(
    (text) => text.toLowerCase().includes(needle),
  );
}

function useActor() {
  const me = useSignedIn();
  return () => (me ? undefined : savedActor().trim() || undefined);
}

function Dialog({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className="catalog-dialog"
      aria-label={title}
      onCancel={close}
      onClose={close}
    >
      <div className="asset-section-heading">
        <h2>{title}</h2>
        <button type="button" className="text-button" onClick={close}>
          关闭
        </button>
      </div>
      {children}
    </dialog>
  );
}

function EditDialog({
  item,
  close,
  saved,
}: {
  item: CatalogItem;
  close: () => void;
  saved: (item: CatalogItem) => void;
}) {
  const actor = useActor();
  const [owner, setOwner] = useState(item.owner_set ? (item.owner ?? "") : "");
  const [tags, setTags] = useState(item.tags.join("、"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    const parsed = parseTags(tags);
    if (parsed.length > MAX_TAGS) return setError(`标签最多 ${MAX_TAGS} 个`);
    if (parsed.some((tag) => tag.length > MAX_TAG))
      return setError(`每个标签 1–${MAX_TAG} 个字符`);
    setBusy(true);
    setError("");
    try {
      saved(await setCatalogMeta(item, owner.trim() || null, parsed, actor()));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <Dialog title={`编辑「${item.title}」`} close={close}>
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
            placeholder={
              item.owner_set ? "" : (item.owner ?? "不填时为新建的人")
            }
            onChange={(event) => setOwner(event.target.value)}
          />
          <small className="muted">不填时为新建的人。</small>
        </label>
        <label className="asset-field">
          标签
          <input
            value={tags}
            disabled={busy}
            placeholder="零售、ETL"
            onChange={(event) => setTags(event.target.value)}
          />
          <small className="muted">
            用顿号或逗号分开，最多 {MAX_TAGS} 个，每个最多 {MAX_TAG} 个字。
          </small>
        </label>
        <div className="asset-actions">
          <button className="button primary" disabled={busy}>
            保存
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={close}
          >
            取消
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function ProposeDialog({
  item,
  close,
  proposed,
}: {
  item: CatalogItem;
  close: () => void;
  proposed: (result: Proposed) => void;
}) {
  const actor = useActor();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Proposed | null>(null);
  async function propose() {
    setBusy(true);
    setError("");
    try {
      const made = await proposeAsset(item, note.trim(), actor());
      setResult(made);
      proposed(made);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={`把「${item.title}」提交到资产仓库`} close={close}>
      {result ? (
        <div className="catalog-result" role="status">
          <p>
            {result.created ? "已开" : "已更新"} PR #{result.number}，改动{" "}
            {result.changed.length} 个文件。
          </p>
          <a href={result.url} target="_blank" rel="noreferrer">
            打开 PR #{result.number} ↗
          </a>
          <div className="asset-actions">
            <button type="button" className="button primary" onClick={close}>
              完成
            </button>
          </div>
        </div>
      ) : (
        <form
          className="asset-form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void propose();
          }}
        >
          {error && <ErrorNotice message={error} />}
          <p className="muted">
            {item.version !== null ? `提交当前版本 v${item.version}。` : ""}
            {item.repo
              ? `会更新已有的 PR #${item.repo.number}。`
              : "会在资产仓库开一个 PR，合不合并由仓库的人决定。"}
          </p>
          <label className="asset-field">
            说明
            <textarea
              rows={3}
              maxLength={500}
              value={note}
              disabled={busy}
              placeholder="这次改了什么（写进 PR 说明，可不填）"
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <div className="asset-actions">
            <button className="button primary" disabled={busy}>
              {busy ? "提交中…" : "提交"}
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={close}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function UsedBy({ item }: { item: CatalogItem }) {
  if (!item.used_by.length) return <span className="muted">没有应用在用</span>;
  return (
    <details className="catalog-uses">
      <summary>{item.used_by.length} 个应用</summary>
      <ul>
        {item.used_by.map((use) => (
          <li key={use.app}>
            <Link href={`/apps/${encodeURIComponent(use.app)}`}>
              {use.title}
            </Link>
            {use.version !== null && (
              <span className="muted"> v{use.version}</span>
            )}
            {use.via && <span className="muted"> · {use.via}</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

export function CatalogList() {
  const [data, setData] = useState<CatalogList | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [kind, setKind] = useState<CatalogKind | "all">("all");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<CatalogItem | null>(null);
  const [proposing, setProposing] = useState<CatalogItem | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    listCatalog(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setData(value);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);
  const searched = useMemo(
    () => (data?.items ?? []).filter((item) => matches(item, query)),
    [data, query],
  );
  const shown = searched.filter((item) => kind === "all" || item.kind === kind);
  const count = (wanted: CatalogKind | "all") =>
    searched.filter((item) => wanted === "all" || item.kind === wanted).length;
  function replace(next: CatalogItem) {
    setData(
      (current) =>
        current && {
          ...current,
          items: current.items.map((item) =>
            key(item) === key(next) ? next : item,
          ),
        },
    );
  }
  return (
    <div className="asset-page">
      <AssetHeading title="资产目录">
        平台上的模板、应用、Skill、知识库、数据源和评测集，一处看负责人、谁在用和质量。
      </AssetHeading>
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((value) => value + 1)}
        />
      ) : !data ? (
        <AssetLoading />
      ) : (
        <section className="panel catalog-panel" aria-label="资产">
          <div className="panel-toolbar catalog-toolbar">
            <div className="filter-tabs" role="group" aria-label="筛选类别">
              {[["all", "全部"] as const, ...catalogKinds].map(
                ([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={kind === value}
                    className={kind === value ? "selected" : ""}
                    onClick={() => setKind(value)}
                  >
                    {label} {count(value)}
                  </button>
                ),
              )}
            </div>
            <input
              type="search"
              className="catalog-search"
              aria-label="搜索资产"
              placeholder="搜名称、说明、负责人或标签"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button
              type="button"
              className="icon-button"
              aria-label="刷新资产目录"
              onClick={() => setRevision((value) => value + 1)}
            >
              <Icon name="refresh" />
            </button>
          </div>
          {!data.repo_ready && (
            <p className="muted catalog-note">
              没配资产仓库，暂时不能把资产提交到资产仓库。
            </p>
          )}
          {!shown.length ? (
            <EmptyState title="没有找到资产">换个类别或搜索词试试。</EmptyState>
          ) : (
            <div className="table-scroll">
              <table
                className="mapping-table catalog-table"
                aria-label="资产列表"
              >
                <thead>
                  <tr>
                    <th scope="col">名称</th>
                    <th scope="col">类别</th>
                    <th scope="col">负责人</th>
                    <th scope="col">版本</th>
                    <th scope="col">被谁用</th>
                    <th scope="col">质量分</th>
                    <th scope="col">资产仓库</th>
                    <th scope="col">
                      <span className="sr-only">操作</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((item) => (
                    <tr key={key(item)} aria-label={item.title}>
                      <td className="catalog-name">
                        <Link href={item.link}>{item.title}</Link>
                        {item.summary && (
                          <small className="muted">{item.summary}</small>
                        )}
                        {item.tags.length > 0 && (
                          <span className="catalog-tags">
                            {item.tags.map((tag) => (
                              <span className="app-tag" key={tag}>
                                {tag}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td>{kindLabel(item.kind)}</td>
                      <td className={item.owner_set ? "" : "muted"}>
                        {item.owner ?? "未指定"}
                      </td>
                      <td>
                        {item.version === null ? "—" : `v${item.version}`}
                      </td>
                      <td>
                        <UsedBy item={item} />
                      </td>
                      <td title={item.quality.detail || undefined}>
                        <strong>{percent(item.quality.score)}</strong>
                        <small className="muted">{item.quality.basis}</small>
                      </td>
                      <td>
                        {item.repo ? (
                          <a
                            href={item.repo.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            PR #{item.repo.number} ↗
                          </a>
                        ) : (
                          <span className="muted">
                            {item.can_propose ? "未提交" : "—"}
                          </span>
                        )}
                      </td>
                      <td className="catalog-actions">
                        <button
                          type="button"
                          className="text-button"
                          aria-label={`编辑「${item.title}」`}
                          onClick={() => setEditing(item)}
                        >
                          编辑
                        </button>
                        {item.can_propose && (
                          <button
                            type="button"
                            className="text-button"
                            aria-label={`把「${item.title}」提交到资产仓库`}
                            disabled={!data.repo_ready}
                            title={data.repo_ready ? undefined : "没配资产仓库"}
                            onClick={() => setProposing(item)}
                          >
                            提交到资产仓库
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
      {editing && (
        <EditDialog
          item={editing}
          close={() => setEditing(null)}
          saved={(next) => {
            replace(next);
            setEditing(null);
          }}
        />
      )}
      {proposing && (
        <ProposeDialog
          item={proposing}
          close={() => setProposing(null)}
          proposed={(made) =>
            replace({
              ...proposing,
              repo: {
                number: made.number,
                url: made.url,
                at: new Date().toISOString(),
                by: null,
              },
            })
          }
        />
      )}
    </div>
  );
}
