"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useT } from "../lib/i18n/client";
import {
  deleteMediaItem,
  deleteMediaLibrary,
  forgetMediaLibraries,
  formatBytes,
  getMediaLibrary,
  listMediaItems,
  MEDIA_KINDS,
  mediaAccept,
  mediaFileUrl,
  mediaPosterUrl,
  mediaKindLabels,
  mediaStatusLabels,
  mediaUploadProblem,
  splitMediaTags,
  updateMediaItem,
  updateMediaLibrary,
  uploadMediaWithProgress,
  type MediaItem,
  type MediaKind,
  type MediaLibrary,
} from "../lib/media";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { MediaLibraryDialog } from "./media-list";
import { MediaPreview, mediaMeta } from "./media-ui";
import { EmptyState, ErrorNotice } from "./ui";

const PAGE = 48;

function EditItemDialog({
  item,
  close,
  saved,
}: {
  item: MediaItem;
  close: () => void;
  saved: (item: MediaItem) => void;
}) {
  const t = useT();
  const dialog = useRef<HTMLDialogElement>(null);
  const [description, setDescription] = useState(item.description);
  const [tags, setTags] = useState(item.tags.join(", "));
  const [usable, setUsable] = useState(item.usable);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  async function save() {
    const list = splitMediaTags(tags);
    if (list.length > 20 || list.some((tag) => tag.length > 20)) {
      setError(t("标签最多 20 个，每个 1–20 个字"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      saved(
        await updateMediaItem(item.library_id, item.item_id, {
          description,
          tags: list,
          usable,
        }),
      );
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="knowledge-dialog"
      aria-labelledby="media-item-dialog-title"
      onCancel={close}
      onClose={close}
    >
      <div className="asset-section-heading">
        <h2 id="media-item-dialog-title">{item.name}</h2>
        <button type="button" className="text-button" onClick={close}>
          {t("关闭")}
        </button>
      </div>
      <form
        className="asset-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div className="asset-field">
          <label htmlFor="media-item-description">{t("说明")}</label>
          <textarea
            id="media-item-description"
            rows={3}
            maxLength={500}
            value={description}
            disabled={busy}
            placeholder={t("例如：健身房杠铃卧推，侧面全景")}
            onChange={(event) => setDescription(event.target.value)}
          />
          <span className="field-hint">
            {t("流程按说明和标签找素材，写清楚画面里有什么。")}
          </span>
        </div>
        <div className="asset-field">
          <label htmlFor="media-item-tags">{t("标签")}</label>
          <input
            id="media-item-tags"
            value={tags}
            disabled={busy}
            placeholder={t("用逗号分开，例如：胸, 卧推")}
            onChange={(event) => setTags(event.target.value)}
          />
        </div>
        <label className="check-field">
          <input
            type="checkbox"
            checked={usable}
            disabled={busy}
            onChange={(event) => setUsable(event.target.checked)}
          />
          {t("流程可以选用它")}
        </label>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <button className="button primary" type="submit" disabled={busy}>
          {busy ? t("正在保存…") : t("保存")}
        </button>
      </form>
    </dialog>
  );
}

function sourceText(t: ReturnType<typeof useT>, item: MediaItem): string {
  if (item.source.kind === "run") return t("运行产出");
  if (item.source.kind === "stock")
    return item.source.site
      ? t("素材站：{site}", { site: item.source.site })
      : t("素材站");
  return item.uploaded_by
    ? t("{name} 上传", { name: item.uploaded_by })
    : t("上传");
}

export function MediaDetail({ id }: { id: string }) {
  const t = useT();
  const router = useRouter();
  const [library, setLibrary] = useState<MediaLibrary | null>(null);
  const [items, setItems] = useState<MediaItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<MediaKind | "">("");
  const [usable, setUsable] = useState<"" | "true" | "false">("");
  const [limit, setLimit] = useState(PAGE);
  const [progress, setProgress] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState<MediaItem | null>(null);
  const [renaming, setRenaming] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abort = useRef<(() => void) | null>(null);
  useEffect(() => () => abort.current?.(), []);

  const processing = items?.some((item) => item.status === "processing");
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      Promise.all([
        getMediaLibrary(id, controller.signal),
        listMediaItems(
          id,
          {
            q: query.trim(),
            kind: kind || undefined,
            usable: usable === "" ? undefined : usable === "true",
            limit,
          },
          controller.signal,
        ),
      ])
        .then(([found, page]) => {
          if (controller.signal.aborted) return;
          setLibrary(found);
          setItems(page.items);
          setTotal(page.total);
          setError("");
        })
        .catch((err) => {
          if (!controller.signal.aborted) setError(errorMessage(err));
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [id, query, kind, usable, limit, revision]);
  // Items being probed turn ready or failed by themselves: look again until they have.
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(() => setRevision((n) => n + 1), 5000);
    return () => clearInterval(timer);
  }, [processing]);

  function upload(files: File[]) {
    if (progress !== null) return;
    const problem = mediaUploadProblem(t, files);
    if (problem) {
      setError(problem);
      return;
    }
    setError("");
    setNotice("");
    setProgress(0);
    const job = uploadMediaWithProgress(t, id, files, {}, setProgress);
    abort.current = job.abort;
    job.done
      .then((made) => {
        setNotice(t("上传了 {n} 个素材。", { n: made.length }));
        forgetMediaLibraries();
        setRevision((n) => n + 1);
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => {
        abort.current = null;
        setProgress(null);
      });
  }

  async function remove(item: MediaItem) {
    if (
      !window.confirm(
        t("确定删除「{name}」？文件会一起删掉。", { name: item.name }),
      )
    )
      return;
    try {
      await deleteMediaItem(id, item.item_id);
      setRevision((n) => n + 1);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function removeLibrary() {
    if (!library) return;
    if (
      !window.confirm(
        t("确定删除媒体库「{name}」？库里的 {n} 个素材和文件会一起删掉。", {
          name: library.name,
          n: library.item_count,
        }),
      )
    )
      return;
    try {
      await deleteMediaLibrary(id);
      forgetMediaLibraries();
      router.push("/media");
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  if (!library)
    return error ? (
      <ErrorNotice message={error} onRetry={() => setRevision((n) => n + 1)} />
    ) : (
      <AssetLoading />
    );
  return (
    <>
      <Link className="back-link" href="/media">
        {t("← 媒体库")}
      </Link>
      <AssetHeading
        title={library.name}
        action={
          <div className="media-actions">
            <button
              className="button secondary"
              onClick={() => setRenaming(true)}
            >
              {t("改名称和说明")}
            </button>
            <button className="button reject" onClick={removeLibrary}>
              {t("删除媒体库")}
            </button>
          </div>
        }
      >
        {library.description || t("暂无说明")}
      </AssetHeading>
      <div className="knowledge-overview">
        <span>{t("{n} 个素材", { n: library.item_count })}</span>
        <span>{formatBytes(library.total_bytes)}</span>
        {library.status_counts.processing > 0 && (
          <span>
            {t("{n} 个处理中", { n: library.status_counts.processing })}
          </span>
        )}
        {library.status_counts.error > 0 && (
          <span>{t("{n} 个处理失败", { n: library.status_counts.error })}</span>
        )}
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {notice && (
        <p className="muted" role="status">
          {notice}
        </p>
      )}
      <section className="panel knowledge-section" aria-label={t("素材")}>
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
            upload(Array.from(event.dataTransfer.files));
          }}
        >
          <input
            ref={fileInput}
            className="sr-only"
            id="media-upload"
            aria-label={t("上传素材")}
            type="file"
            multiple
            accept={mediaAccept()}
            disabled={progress !== null}
            onChange={(event) => {
              upload(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
          <button
            className="button secondary"
            disabled={progress !== null}
            onClick={() => fileInput.current?.click()}
          >
            {progress !== null
              ? t("正在上传 {n}%", { n: progress })
              : t("点击上传素材")}
          </button>
          {progress !== null && (
            <progress max={100} value={progress} aria-label={t("上传进度")} />
          )}
          <p>{t("或将文件拖拽到这里")}</p>
          <small className="muted">
            {t(
              "视频 MP4 / MOV / M4V / WEBM / MKV · 音频 M4A / MP3 / WAV / AAC / OGG / OPUS / FLAC · 图片 JPG / PNG / WEBP · 每次最多 20 个",
            )}
          </small>
        </div>
        <div className="media-filters">
          <input
            type="search"
            aria-label={t("搜索素材")}
            placeholder={t("按名称、说明或标签搜索")}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setLimit(PAGE);
            }}
          />
          <select
            aria-label={t("素材类型")}
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as MediaKind | "");
              setLimit(PAGE);
            }}
          >
            <option value="">{t("全部类型")}</option>
            {MEDIA_KINDS.map((value) => (
              <option key={value} value={value}>
                {t(mediaKindLabels[value])}
              </option>
            ))}
          </select>
          <select
            aria-label={t("能否选用")}
            value={usable}
            onChange={(event) => {
              setUsable(event.target.value as "" | "true" | "false");
              setLimit(PAGE);
            }}
          >
            <option value="">{t("全部素材")}</option>
            <option value="true">{t("流程可以选用")}</option>
            <option value="false">{t("流程不选用")}</option>
          </select>
          <span className="muted">{t("共 {n} 个", { n: total })}</span>
        </div>
        {items?.length === 0 && (
          <EmptyState title={t("没有素材")}>
            {query || kind || usable
              ? t("换个条件再找找。")
              : t("上传视频、音频或图片后会显示在这里。")}
          </EmptyState>
        )}
        <ul className="media-grid" aria-label={t("素材列表")}>
          {items?.map((item) => (
            <li className="media-card" key={item.item_id}>
              <MediaPreview
                kind={item.kind}
                src={mediaFileUrl(item.library_id, item.item_id)}
                name={item.name}
                poster={mediaPosterUrl(item)}
              />
              <div className="media-card-body">
                <strong className="media-name" title={item.name}>
                  {item.name}
                </strong>
                <span className="muted">{mediaMeta(t, item)}</span>
                {item.description && <p>{item.description}</p>}
                {item.tags.length > 0 && (
                  <div className="media-tags">
                    {item.tags.map((tag) => (
                      <span className="app-tag" key={tag}>
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
                <span className="muted">
                  {sourceText(t, item)} · <AssetTime value={item.created_at} />
                </span>
                {item.status !== "ready" && (
                  <span className={`knowledge-status ${item.status}`}>
                    {t(mediaStatusLabels[item.status])}
                  </span>
                )}
                {item.error && <p className="field-error">{item.error}</p>}
                {!item.usable && (
                  <span className="muted">{t("流程不选用")}</span>
                )}
                <div className="media-card-actions">
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setEditing(item)}
                  >
                    {t("编辑")}
                  </button>
                  <a
                    className="text-button"
                    href={mediaFileUrl(item.library_id, item.item_id)}
                    download={item.name}
                  >
                    {t("下载")}
                  </a>
                  <button
                    type="button"
                    className="text-button danger-text"
                    onClick={() => void remove(item)}
                  >
                    {t("删除")}
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
        {items && items.length < total && (
          <button
            className="button secondary"
            onClick={() => setLimit((n) => Math.min(n + PAGE, 200))}
            disabled={limit >= 200}
          >
            {limit >= 200
              ? t("最多显示 200 个，请用搜索缩小范围")
              : t("加载更多")}
          </button>
        )}
      </section>
      {editing && (
        <EditItemDialog
          item={editing}
          close={() => setEditing(null)}
          saved={(item) => {
            setItems(
              (old) =>
                old?.map((x) => (x.item_id === item.item_id ? item : x)) ??
                null,
            );
            setEditing(null);
          }}
        />
      )}
      {renaming && (
        <MediaLibraryDialog
          title={t("改名称和说明")}
          library={library}
          close={() => setRenaming(false)}
          save={async (input) => {
            setLibrary(await updateMediaLibrary(id, input));
            forgetMediaLibraries();
            setRenaming(false);
          }}
        />
      )}
    </>
  );
}
