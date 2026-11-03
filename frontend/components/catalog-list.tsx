"use client";

import { useT, type Translate } from "../lib/i18n/client";
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

// The singular name of a kind ("App"); the filter tabs use the plural.
const kindName = (t: Translate, kind: CatalogKind) =>
  t(`${kindLabel(kind)}#类别`); // i18n-ignore

const key = (item: Pick<CatalogItem, "kind" | "id">) =>
  `${item.kind}:${item.id}`;

function matches(item: CatalogItem, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [item.title, item.summary, item.owner ?? "", ...item.tags].some(
    (text) => text.toLowerCase().includes(needle),
  );
}

export function useActor() {
  const me = useSignedIn();
  return () => (me ? undefined : savedActor().trim() || undefined);
}

export function Dialog({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const t = useT();
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
          {t("关闭")}
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
  const t = useT();
  const actor = useActor();
  const [owner, setOwner] = useState(item.owner_set ? (item.owner ?? "") : "");
  const [tags, setTags] = useState(item.tags.join("、"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    const parsed = parseTags(tags);
    if (parsed.length > MAX_TAGS)
      return setError(t("标签最多 {MAX_TAGS} 个", { MAX_TAGS }));
    if (parsed.some((tag) => tag.length > MAX_TAG))
      return setError(t("每个标签 1–{MAX_TAG} 个字符", { MAX_TAG }));
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
    <Dialog title={t("编辑「{title}」", { title: item.title })} close={close}>
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
          {t("负责人")}
          <input
            maxLength={50}
            value={owner}
            disabled={busy}
            placeholder={
              item.owner_set ? "" : (item.owner ?? t("不填时为新建的人"))
            }
            onChange={(event) => setOwner(event.target.value)}
          />
          <small className="muted">{t("不填时为新建的人。")}</small>
        </label>
        <label className="asset-field">
          {t("标签")}
          <input
            value={tags}
            disabled={busy}
            placeholder={t("零售、ETL")}
            onChange={(event) => setTags(event.target.value)}
          />
          <small className="muted">
            {t("用顿号或逗号分开，最多 {max} 个，每个最多 {chars} 个字。", {
              max: MAX_TAGS,
              chars: MAX_TAG,
            })}
          </small>
        </label>
        <div className="asset-actions">
          <button className="button primary" disabled={busy}>
            {t("保存")}
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={close}
          >
            {t("取消")}
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
  const t = useT();
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
    <Dialog
      title={t("把「{title}」提交到资产仓库", { title: item.title })}
      close={close}
    >
      {result ? (
        <div className="catalog-result" role="status">
          <p>
            {t(
              result.created
                ? "已开 PR #{number}，改动 {n} 个文件。"
                : "已更新 PR #{number}，改动 {n} 个文件。",
              { number: result.number, n: result.changed.length },
            )}
          </p>
          <a href={result.url} target="_blank" rel="noreferrer">
            {t("打开 PR #{number} ↗", { number: result.number })}
          </a>
          <div className="asset-actions">
            <button type="button" className="button primary" onClick={close}>
              {t("完成")}
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
            {item.version !== null
              ? t("提交当前版本 v{version}。", { version: item.version })
              : ""}
            {item.repo
              ? t("会更新已有的 PR #{number}。", { number: item.repo.number })
              : t("会在资产仓库开一个 PR，合不合并由仓库的人决定。")}
          </p>
          <label className="asset-field">
            {t("说明")}
            <textarea
              rows={3}
              maxLength={500}
              value={note}
              disabled={busy}
              placeholder={t("这次改了什么（写进 PR 说明，可不填）")}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <div className="asset-actions">
            <button className="button primary" disabled={busy}>
              {busy ? t("提交中…") : t("提交")}
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={close}
            >
              {t("取消")}
            </button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function UsedBy({ item }: { item: CatalogItem }) {
  const t = useT();
  if (!item.used_by.length)
    return <span className="muted">{t("没有应用在用")}</span>;
  return (
    <details className="catalog-uses">
      <summary>{t("{n} 个应用", { n: item.used_by.length })}</summary>
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
  const t = useT();
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
      <AssetHeading title={t("资产目录")}>
        {t(
          "平台上的模板、应用、Skill、知识库、数据源和评测集，一处看负责人、谁在用和质量。",
        )}
      </AssetHeading>
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((value) => value + 1)}
        />
      ) : !data ? (
        <AssetLoading />
      ) : (
        <section className="panel catalog-panel" aria-label={t("资产")}>
          <div className="panel-toolbar catalog-toolbar">
            <div
              className="filter-tabs"
              role="group"
              aria-label={t("筛选类别")}
            >
              {[["all", t("全部")] as const, ...catalogKinds].map(
                ([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={kind === value}
                    className={kind === value ? "selected" : ""}
                    onClick={() => setKind(value)}
                  >
                    {t(label)}{" "}
                    <span className="filter-count">{count(value)}</span>
                  </button>
                ),
              )}
            </div>
            <input
              type="search"
              className="catalog-search"
              aria-label={t("搜索资产")}
              placeholder={t("搜名称、说明、负责人或标签")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button
              type="button"
              className="icon-button"
              aria-label={t("刷新资产目录")}
              onClick={() => setRevision((value) => value + 1)}
            >
              <Icon name="refresh" />
            </button>
          </div>
          {!data.repo_ready && (
            <p className="muted catalog-note">
              {t("没配资产仓库，暂时不能把资产提交到资产仓库。")}
            </p>
          )}
          {!shown.length ? (
            <EmptyState title={t("没有找到资产")}>
              {t("换个类别或搜索词试试。")}
            </EmptyState>
          ) : (
            <div className="table-scroll">
              <table
                className="mapping-table catalog-table"
                aria-label={t("资产列表")}
              >
                <thead>
                  <tr>
                    <th scope="col">{t("名称")}</th>
                    <th scope="col">{t("类别")}</th>
                    <th scope="col">{t("负责人")}</th>
                    <th scope="col">{t("被谁用")}</th>
                    <th scope="col">{t("质量分")}</th>
                    <th scope="col">
                      <span className="sr-only">{t("操作")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((item) => (
                    <tr key={key(item)} aria-label={item.title}>
                      <td className="catalog-name">
                        <Link href={item.link}>{item.title}</Link>
                        {item.version !== null && (
                          <span className="catalog-version">
                            v{item.version}
                          </span>
                        )}
                        {item.summary && (
                          <small className="muted" title={item.summary}>
                            {item.summary}
                          </small>
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
                      <td>{kindName(t, item.kind)}</td>
                      <td className={item.owner_set ? "" : "muted"}>
                        {item.owner ?? t("未指定")}
                      </td>
                      <td>
                        <UsedBy item={item} />
                      </td>
                      <td title={item.quality.detail || undefined}>
                        <strong>{t(percent(item.quality.score))}</strong>
                        <small className="muted">{item.quality.basis}</small>
                      </td>
                      <td className="catalog-actions">
                        <button
                          type="button"
                          className="text-button"
                          aria-label={t("编辑「{title}」", {
                            title: item.title,
                          })}
                          onClick={() => setEditing(item)}
                        >
                          {t("编辑")}
                        </button>
                        {item.can_propose && (
                          <button
                            type="button"
                            className="text-button"
                            aria-label={t("把「{title}」提交到资产仓库", {
                              title: item.title,
                            })}
                            disabled={!data.repo_ready}
                            title={
                              data.repo_ready ? undefined : t("没配资产仓库")
                            }
                            onClick={() => setProposing(item)}
                          >
                            {item.repo ? t("更新 PR") : t("提交")}
                          </button>
                        )}
                        {item.repo && (
                          <a
                            href={item.repo.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            PR #{item.repo.number} ↗
                          </a>
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
