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
  mediaPosterUrl,
  mediaKindLabels,
  mediaUploadProblem,
  uploadMediaWithProgress,
  type MediaItem,
  type MediaKind,
} from "../lib/media";
import { ApiError, errorMessage } from "../lib/runs";
import { useMay } from "./session";

// Plays or shows an item where it is; the browser asks the proxy for byte ranges.
export function MediaPreview({
  kind,
  src,
  name,
  poster,
}: {
  kind: MediaKind;
  src: string;
  name: string;
  // A thumbnail: the video shows it and loads nothing until it plays.
  poster?: string;
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
      poster={poster}
      controls
      preload={poster ? "none" : "metadata"}
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

const PICK_PAGE = 50;
// How often an upload the connector is still probing is looked at again: soon at
// first, then less often (the platform retries a minute later when it is away).
const CHECK_FIRST_MS = 1000;
const CHECK_SOON_MS = 2000;
const CHECK_LATER_MS = 5000;
const CHECK_SOON_TIMES = 15;

// A start input of type media on the new-run page: pick an item of the input's library
// (newest first, searchable), or upload one and use it once it is ready (a run takes
// only ready items; with the media connector set an upload is probed first).
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
  // The items that can be picked, and how far through the library's list they reach.
  const [items, setItems] = useState<MediaItem[] | null>(null);
  const [read, setRead] = useState({ fetched: 0, total: 0 });
  const [loadingMore, setLoadingMore] = useState(false);
  const [chosen, setChosen] = useState<MediaItem | null>(null);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  // An upload still being probed: chosen when it is ready, never before.
  const [checking, setChecking] = useState<MediaItem | null>(null);
  const [revision, setRevision] = useState(0);
  const file = useRef<HTMLInputElement>(null);
  const abort = useRef<(() => void) | null>(null);
  const listing = useRef<AbortController | null>(null);
  // An upload ends after other inputs may have changed: use the form's latest handler.
  const latestChange = useRef(onChange);
  useEffect(() => {
    latestChange.current = onChange;
  });
  useEffect(() => () => abort.current?.(), []);
  useEffect(() => {
    if (!checking) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let looked = 0;
    const later = () => {
      looked += 1;
      timer = setTimeout(
        look,
        looked < CHECK_SOON_TIMES ? CHECK_SOON_MS : CHECK_LATER_MS,
      );
    };
    function look() {
      getMediaItem(checking!.library_id, checking!.item_id, controller.signal)
        .then((item) => {
          if (controller.signal.aborted) return;
          if (item.status === "processing") return later();
          setChecking(null);
          // Not usable: said so and not chosen (a list reload would clear the error).
          if (item.status === "ready") choose(item);
          else
            setError(
              t("「{name}」用不了：{error}", {
                name: item.name,
                error: item.error ?? t("处理失败了"),
              }),
            );
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          if (err instanceof ApiError && err.status === 404) {
            setChecking(null);
            setError(errorMessage(err));
            return;
          }
          later(); // a passing network problem: look again
        });
    }
    timer = setTimeout(look, CHECK_FIRST_MS);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // `t` and `choose` read only state setters and the latest handler.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checking]);
  function choose(item: MediaItem) {
    setChosen(item);
    latestChange.current(item.item_id);
    setRevision((n) => n + 1);
  }
  const busy = progress !== null || checking !== null;
  const accepted = kinds?.length ? kinds : undefined;
  const oneKind = accepted?.length === 1 ? accepted[0] : undefined;
  const pickable = (page: MediaItem[]) =>
    page.filter(
      (item) =>
        (!accepted || accepted.includes(item.kind)) && item.status === "ready",
    );
  function fetchPage(offset: number) {
    listing.current?.abort();
    const controller = new AbortController();
    listing.current = controller;
    return listMediaItems(
      library,
      { q: query.trim(), kind: oneKind, limit: PICK_PAGE, offset },
      controller.signal,
    ).then(
      (page) => {
        if (controller.signal.aborted) return null;
        setRead({ fetched: offset + page.items.length, total: page.total });
        setError("");
        return pickable(page.items);
      },
      (err) => {
        if (controller.signal.aborted) return null;
        throw err;
      },
    );
  }
  useEffect(() => {
    if (!library) return;
    const timer = setTimeout(() => {
      fetchPage(0)
        .then((found) => found && setItems(found))
        .catch((err) => setError(errorMessage(err)));
    }, 250);
    return () => {
      clearTimeout(timer);
      listing.current?.abort();
      setLoadingMore(false);
    };
    // `accepted` is derived from `kinds`; its text stands in for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [library, query, oneKind, accepted?.join(","), revision]);
  function loadMore() {
    setLoadingMore(true);
    fetchPage(read.fetched)
      .then((found) => found && setItems((had) => [...(had ?? []), ...found]))
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setLoadingMore(false));
  }
  const unread = Math.max(read.total - read.fetched, 0);
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
        if (item.status === "ready") choose(item);
        else if (item.status === "processing") setChecking(item);
        else
          setError(
            t("「{name}」用不了：{error}", {
              name: item.name,
              error: item.error ?? t("处理失败了"),
            }),
          );
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
          disabled={disabled || busy}
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
              disabled={disabled || busy}
              onChange={(event) => {
                upload(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
            />
            <button
              type="button"
              className="button secondary"
              disabled={disabled || busy}
              onClick={() => file.current?.click()}
            >
              {progress !== null
                ? t("正在上传 {n}%", { n: progress })
                : t("上传一个")}
            </button>
          </>
        )}
      </div>
      {checking && (
        <p className="field-hint" role="status">
          {t("正在检查「{name}」，检查完会自动选上。", { name: checking.name })}
        </p>
      )}
      {items !== null && unread > 0 && (
        <p className="field-hint">
          {t("还有 {n} 个素材没列出来。", { n: unread })}{" "}
          <button
            type="button"
            className="text-button"
            disabled={disabled || loadingMore}
            onClick={loadMore}
          >
            {loadingMore ? t("正在加载…") : t("再列出一些")}
          </button>
        </p>
      )}
      {items?.length === 0 && !query && unread === 0 && (
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
          poster={mediaPosterUrl(shown)}
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
