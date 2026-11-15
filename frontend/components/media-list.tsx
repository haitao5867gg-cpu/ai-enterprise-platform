"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useT } from "../lib/i18n/client";
import {
  createMediaLibrary,
  forgetMediaLibraries,
  formatBytes,
  listMediaLibraries,
  type MediaLibrary,
} from "../lib/media";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { EmptyState, ErrorNotice } from "./ui";

// The name and description of a library, for a new one or an edit.
export function MediaLibraryDialog({
  title,
  library,
  close,
  save,
}: {
  title: string;
  library?: Pick<MediaLibrary, "name" | "description">;
  close: () => void;
  save: (input: { name: string; description: string }) => Promise<void>;
}) {
  const t = useT();
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(library?.name ?? "");
  const [description, setDescription] = useState(library?.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  async function submit() {
    if (!name.trim() || name.trim().length > 50) {
      setError(t("名称要 1–50 个字符"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      await save({ name: name.trim(), description });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="knowledge-dialog"
      aria-labelledby="media-library-dialog-title"
      onCancel={close}
      onClose={close}
    >
      <div className="asset-section-heading">
        <h2 id="media-library-dialog-title">{title}</h2>
        <button type="button" className="text-button" onClick={close}>
          {t("关闭")}
        </button>
      </div>
      <form
        className="asset-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="asset-field">
          <label htmlFor="media-library-name">{t("名称")}</label>
          <input
            id="media-library-name"
            value={name}
            maxLength={50}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div className="asset-field">
          <label htmlFor="media-library-description">{t("说明")}</label>
          <textarea
            id="media-library-description"
            rows={3}
            value={description}
            maxLength={500}
            disabled={busy}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
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

export function MediaList() {
  const t = useT();
  const [items, setItems] = useState<MediaLibrary[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    listMediaLibraries(controller.signal)
      .then((libraries) => {
        if (controller.signal.aborted) return;
        setItems(libraries);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);
  return (
    <>
      <AssetHeading
        title={t("媒体库")}
        action={
          <button className="button primary" onClick={() => setCreating(true)}>
            {t("新建媒体库")}
          </button>
        }
      >
        {t(
          "存放视频、音频和图片素材。编排应用绑定媒体库后，运行时可以选库里的素材，外部页面也能经应用 API 上传。",
        )}
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items?.length === 0 && (
        <EmptyState title={t("还没有媒体库")}>
          {t("新建一个媒体库，再上传视频、音频或图片。")}
        </EmptyState>
      )}
      <div className="knowledge-cards">
        {items?.map((library) => (
          <Link
            className="panel knowledge-card"
            href={`/media/${encodeURIComponent(library.library_id)}`}
            key={library.library_id}
          >
            <h2>{library.name}</h2>
            <p className="muted">{library.description || t("暂无说明")}</p>
            <dl className="knowledge-stats">
              <div>
                <dt>{t("素材")}</dt>
                <dd>{library.item_count}</dd>
              </div>
              <div>
                <dt>{t("占用空间")}</dt>
                <dd>{formatBytes(library.total_bytes)}</dd>
              </div>
              <div>
                <dt>{t("处理中")}</dt>
                <dd>{library.status_counts.processing}</dd>
              </div>
            </dl>
            <span className="text-button">{t("查看媒体库 →")}</span>
          </Link>
        ))}
      </div>
      {creating && (
        <MediaLibraryDialog
          title={t("新建媒体库")}
          close={() => setCreating(false)}
          save={async (input) => {
            const created = await createMediaLibrary(input);
            forgetMediaLibraries();
            setItems((old) =>
              [...(old ?? []), created].sort((a, b) =>
                a.name.localeCompare(b.name),
              ),
            );
            setCreating(false);
          }}
        />
      )}
    </>
  );
}
