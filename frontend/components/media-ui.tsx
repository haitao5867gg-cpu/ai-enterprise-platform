"use client";

import { useEffect, useRef, useState } from "react";
import { useT } from "../lib/i18n/client";
import {
  formatBytes,
  formatDuration,
  getMediaItem,
  listMediaItems,
  mediaAccept,
  mediaFileUrl,
  mediaKindLabels,
  mediaUploadProblem,
  uploadMediaWithProgress,
  type MediaItem,
  type MediaKind,
} from "../lib/media";
import { errorMessage } from "../lib/runs";
import { useMay } from "./session";

// Plays or shows an item where it is; the browser asks the proxy for byte ranges.
export function MediaPreview({
  kind,
  src,
  name,
}: {
  kind: MediaKind;
  src: string;
  name: string;
}) {
  if (kind === "image")
    // A user's own file through the proxy: next/image would try to optimise it.
    // eslint-disable-next-line @next/next/no-img-element
    return <img className="media-preview" src={src} alt={name} />;
  if (kind === "audio")
    return (
      <audio
        className="media-preview audio"
        src={src}
        controls
        preload="none"
        aria-label={name}
      />
    );
  return (
    <video
      className="media-preview"
      src={src}
      controls
      preload="metadata"
      playsInline
      aria-label={name}
    />
  );
}

// "视频 · 0:12 · 25.1 MB · 720×1280": what kind of file it is and how big.
export function mediaMeta(
  t: (text: string) => string,
  item: Pick<
    MediaItem,
    "kind" | "duration_seconds" | "size_bytes" | "width" | "height"
  >,
): string {
  return [
    t(mediaKindLabels[item.kind]),
    formatDuration(item.duration_seconds),
    formatBytes(item.size_bytes),
    item.width && item.height ? `${item.width}×${item.height}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

// A start input of type media on the new-run page: pick an item of the input's library
// (newest first, searchable), or upload one and use it.
export function MediaPicker({
  id,
  library,
  kinds,
  value,
  onChange,
  disabled,
  common,
}: {
  id: string;
  library: string;
  kinds?: MediaKind[];
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  common: Record<string, unknown>;
}) {
  const t = useT();
  // Uploading to a library is for builders (docs/api/auth.md); others only pick.
  const mayUpload = useMay("builder");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<MediaItem[] | null>(null);
  const [chosen, setChosen] = useState<MediaItem | null>(null);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const file = useRef<HTMLInputElement>(null);
  const abort = useRef<(() => void) | null>(null);
  useEffect(() => () => abort.current?.(), []);
  const accepted = kinds?.length ? kinds : undefined;
  const oneKind = accepted?.length === 1 ? accepted[0] : undefined;
  useEffect(() => {
    if (!library) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      listMediaItems(
        library,
        { q: query.trim(), kind: oneKind, limit: 50 },
        controller.signal,
      )
        .then((page) => {
          if (controller.signal.aborted) return;
          setItems(
            page.items.filter(
              (item) =>
                (!accepted || accepted.includes(item.kind)) &&
                item.status === "ready",
            ),
          );
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
    // `accepted` is derived from `kinds`; its text stands in for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [library, query, oneKind, accepted?.join(","), revision]);
  // The chosen item, also when it is not in the list (copied from an earlier run).
  const listed = value
    ? items?.find((item) => item.item_id === value)
    : undefined;
  useEffect(() => {
    if (!value || !library || listed || chosen?.item_id === value) return;
    const controller = new AbortController();
    getMediaItem(library, value, controller.signal)
      .then((item) => !controller.signal.aborted && setChosen(item))
      .catch(() => undefined);
    return () => controller.abort();
  }, [value, library, listed, chosen?.item_id]);
  const shown = listed ?? (value && chosen?.item_id === value ? chosen : null);
  const options = [
    ...(shown && !items?.some((item) => item.item_id === shown.item_id)
      ? [shown]
      : []),
    ...(items ?? []),
  ];

  function upload(files: File[]) {
    const problem = mediaUploadProblem(t, files.slice(0, 1), accepted);
    if (problem) {
      setError(problem);
      return;
    }
    setError("");
    setProgress(0);
    const job = uploadMediaWithProgress(
      t,
      library,
      files.slice(0, 1),
      {},
      setProgress,
    );
    abort.current = job.abort;
    job.done
      .then(([item]) => {
        setChosen(item);
        onChange(item.item_id);
        setRevision((n) => n + 1);
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => {
        abort.current = null;
        setProgress(null);
      });
  }

  if (!library)
    return <p className="field-error">{t("这个输入没有选媒体库")}</p>;
  return (
    <div className="media-picker">
      <div className="media-picker-row">
        <input
          type="search"
          className="media-picker-search"
          aria-label={t("搜索素材")}
          placeholder={t("按名称、说明或标签搜索")}
          value={query}
          disabled={disabled}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          {...common}
          id={id}
          value={value}
          disabled={disabled || progress !== null}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">
            {items === null ? t("正在加载…") : t("请选择素材")}
          </option>
          {options.map((item) => (
            <option key={item.item_id} value={item.item_id}>
              {`${item.name} · ${mediaMeta(t, item)}`}
            </option>
          ))}
        </select>
        {mayUpload && (
          <>
            <input
              ref={file}
              type="file"
              className="sr-only"
              aria-label={t("上传一个素材")}
              accept={mediaAccept(accepted)}
              disabled={disabled || progress !== null}
              onChange={(event) => {
                upload(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
            />
            <button
              type="button"
              className="button secondary"
              disabled={disabled || progress !== null}
              onClick={() => file.current?.click()}
            >
              {progress !== null
                ? t("正在上传 {n}%", { n: progress })
                : t("上传一个")}
            </button>
          </>
        )}
      </div>
      {items?.length === 0 && !query && (
        <p className="field-hint">
          {mayUpload
            ? t("库里还没有能用的素材，先上传一个。")
            : t("库里还没有能用的素材，请搭建者先上传。")}
        </p>
      )}
      {shown && (
        <MediaPreview
          kind={shown.kind}
          src={mediaFileUrl(shown.library_id, shown.item_id)}
          name={shown.name}
        />
      )}
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
