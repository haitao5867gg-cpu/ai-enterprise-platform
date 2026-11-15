import type { Translate } from "./i18n/client";
import { ApiError, requestJson } from "./runs";

// docs/api/media.md is the source of truth for these wire types.
export type MediaKind = "video" | "audio" | "image";
export type MediaStatus = "processing" | "ready" | "error";
export type MediaLibrary = {
  library_id: string;
  name: string;
  description: string;
  item_count: number;
  total_bytes: number;
  status_counts: Record<MediaStatus, number>;
  created_at: string;
  updated_at: string;
};
export type MediaSource = {
  kind: "upload" | "stock" | "run";
  site?: string | null;
  url?: string | null;
  author?: string | null;
  license?: string | null;
  license_url?: string | null;
  run_id?: string | null;
};
export type MediaItem = {
  item_id: string;
  library_id: string;
  name: string;
  kind: MediaKind;
  size_bytes: number;
  status: MediaStatus;
  error: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  orientation: "portrait" | "landscape" | "square" | null;
  description: string;
  tags: string[];
  usable: boolean;
  source: MediaSource;
  uploaded_by: string | null;
  // A JPG the media processing connector made (docs/api/media-processing.md).
  has_thumbnail: boolean;
  created_at: string;
  updated_at: string;
};
export type MediaPage = { items: MediaItem[]; total: number };
export type MediaQuery = {
  q?: string;
  tag?: string;
  kind?: MediaKind;
  usable?: boolean;
  limit?: number;
  offset?: number;
};
export type MediaChanges = Partial<
  Pick<MediaItem, "description" | "tags" | "usable">
>;
// A file a run made into a library its app binds (docs/api/media.md, 结果里的文件).
export type RunMediaFile = {
  item_id: string;
  library_id: string;
  name: string;
  kind: MediaKind;
  size_bytes: number;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  download_path: string;
};
// How a flow app may use a library it binds (the configuration's `media.libraries`).
export type MediaAccess = "read" | "write";
export type MediaBinding = { library_id: string; access: MediaAccess };

// Tags typed as one line: split on commas (half or full width) and spaces.
export const splitMediaTags = (text: string) => [
  ...new Set(
    text
      .split(/[,，、\s]+/)
      .map((tag) => tag.trim())
      .filter(Boolean),
  ),
];

export const MEDIA_KINDS: MediaKind[] = ["video", "audio", "image"];
export const mediaKindLabels: Record<MediaKind, string> = {
  video: "视频",
  audio: "音频",
  image: "图片",
};
export const mediaStatusLabels: Record<MediaStatus, string> = {
  processing: "处理中",
  ready: "可用",
  error: "处理失败",
};
export const mediaAccessLabels: Record<MediaAccess, string> = {
  read: "只读",
  write: "可以上传和修改",
};
export const MAX_BINDINGS = 10;
export const MAX_UPLOAD = 20;
const EXTENSIONS: Record<MediaKind, string[]> = {
  video: ["mp4", "mov", "m4v", "webm", "mkv"],
  audio: ["m4a", "mp3", "wav", "aac", "ogg", "opus", "flac"],
  image: ["jpg", "jpeg", "png", "webp"],
};
// What the file picker offers: the extensions of the kinds asked for (all by default).
export function mediaAccept(kinds: MediaKind[] = MEDIA_KINDS): string {
  return kinds
    .flatMap((kind) => EXTENSIONS[kind].map((e) => `.${e}`))
    .join(",");
}
export function mediaKindOf(name: string): MediaKind | null {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return MEDIA_KINDS.find((kind) => EXTENSIONS[kind].includes(ext)) ?? null;
}
// The first thing wrong with a batch before it is sent, or "". The server checks again.
export function mediaUploadProblem(
  t: Translate,
  files: Pick<File, "name" | "size">[],
  kinds: MediaKind[] = MEDIA_KINDS,
): string {
  if (!files.length) return t("请选择至少一个视频、音频或图片文件");
  if (files.length > MAX_UPLOAD)
    return t("单次最多上传 {n} 个文件", { n: MAX_UPLOAD });
  for (const file of files) {
    const kind = mediaKindOf(file.name);
    if (!kind || !kinds.includes(kind))
      return t("文件「{name}」：这里只收{kinds}", {
        name: file.name,
        kinds: kinds.map((k) => t(mediaKindLabels[k])).join(t("、")),
      });
    if (!file.size) return t("文件「{name}」：是空文件", { name: file.name });
  }
  return "";
}
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "";
  const whole = Math.round(seconds);
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});
const base = (library: string) =>
  `/media-libraries/${encodeURIComponent(library)}`;
const item = (library: string, id: string) =>
  `${base(library)}/items/${encodeURIComponent(id)}`;

export const listMediaLibraries = (signal?: AbortSignal) =>
  requestJson("/media-libraries", { signal }) as Promise<MediaLibrary[]>;
export const createMediaLibrary = (input: {
  name: string;
  description?: string;
}) =>
  requestJson("/media-libraries", json("POST", input)) as Promise<MediaLibrary>;
export const getMediaLibrary = (library: string, signal?: AbortSignal) =>
  requestJson(base(library), { signal }) as Promise<MediaLibrary>;
export const updateMediaLibrary = (
  library: string,
  input: { name?: string; description?: string },
) => requestJson(base(library), json("PATCH", input)) as Promise<MediaLibrary>;
export const deleteMediaLibrary = (library: string) =>
  requestJson(base(library), json("DELETE"));
export function listMediaItems(
  library: string,
  query: MediaQuery = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query))
    if (value !== undefined && value !== "") params.set(key, String(value));
  const search = params.toString();
  return requestJson(`${base(library)}/items${search ? `?${search}` : ""}`, {
    signal,
  }) as Promise<MediaPage>;
}
export function uploadMediaItems(
  library: string,
  files: File[],
  options: { description?: string; tags?: string[]; uploadedBy?: string } = {},
) {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  if (options.description) body.append("description", options.description);
  if (options.tags?.length) body.append("tags", JSON.stringify(options.tags));
  if (options.uploadedBy) body.append("uploaded_by", options.uploadedBy);
  return requestJson(`${base(library)}/items`, {
    method: "POST",
    body,
  }) as Promise<MediaItem[]>;
}
export const getMediaItem = (
  library: string,
  id: string,
  signal?: AbortSignal,
) => requestJson(item(library, id), { signal }) as Promise<MediaItem>;
export const updateMediaItem = (
  library: string,
  id: string,
  changes: MediaChanges,
) =>
  requestJson(item(library, id), json("PATCH", changes)) as Promise<MediaItem>;
export const deleteMediaItem = (library: string, id: string) =>
  requestJson(item(library, id), json("DELETE"));
// Where the page plays or downloads an item: through the platform's proxy.
export const mediaFileUrl = (library: string, id: string) =>
  `/api${item(library, id)}/file`;
// The item's thumbnail, when it has one: a video shows it until it plays.
export const mediaPosterUrl = (
  media: Pick<MediaItem, "library_id" | "item_id" | "has_thumbnail">,
) =>
  media.has_thumbnail
    ? `/api${item(media.library_id, media.item_id)}/thumbnail`
    : undefined;
export const listRunMedia = (runId: string, signal?: AbortSignal) =>
  requestJson(`/runs/${encodeURIComponent(runId)}/media`, {
    signal,
  }) as Promise<RunMediaFile[]>;

// The libraries, fetched once per page load and shared by the pickers that need them.
let cached: Promise<MediaLibrary[]> | null = null;
export function cachedMediaLibraries(): Promise<MediaLibrary[]> {
  cached ??= listMediaLibraries().catch((err) => {
    cached = null;
    throw err;
  });
  return cached;
}
export function forgetMediaLibraries() {
  cached = null;
}

// An upload that reports how far it got: a video can take minutes. The server's own
// message comes back in the reader's language; ours go through ``t``.
export function uploadMediaWithProgress(
  t: Translate,
  library: string,
  files: File[],
  options: { description?: string; tags?: string[]; uploadedBy?: string },
  onProgress: (percent: number) => void,
): { done: Promise<MediaItem[]>; abort: () => void } {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  if (options.description) body.append("description", options.description);
  if (options.tags?.length) body.append("tags", JSON.stringify(options.tags));
  if (options.uploadedBy) body.append("uploaded_by", options.uploadedBy);
  const xhr = new XMLHttpRequest();
  const done = new Promise<MediaItem[]>((resolve, reject) => {
    xhr.open("POST", `/api${base(library)}/items`);
    xhr.timeout = 3_600_000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      let value: unknown = null;
      try {
        value = JSON.parse(xhr.responseText);
      } catch {
        value = null;
      }
      if (xhr.status >= 200 && xhr.status < 300 && Array.isArray(value)) {
        resolve(value as MediaItem[]);
        return;
      }
      const detail =
        typeof value === "object" && value !== null && "detail" in value
          ? value.detail
          : null;
      const first =
        Array.isArray(detail) &&
        typeof detail[0] === "object" &&
        detail[0] !== null &&
        "msg" in detail[0]
          ? String(detail[0].msg)
          : null;
      reject(
        new ApiError(
          typeof detail === "string"
            ? detail
            : (first ??
                (xhr.status === 403
                  ? t("没有权限做这件事。")
                  : t("上传失败，请重试。"))),
          xhr.status,
        ),
      );
    };
    xhr.onerror = () => reject(new ApiError(t("上传连接中断，请重试。")));
    xhr.ontimeout = () => reject(new ApiError(t("上传超时，请重试。")));
    xhr.onabort = () => reject(new ApiError(t("上传已取消。")));
    xhr.send(body);
  });
  return { done, abort: () => xhr.abort() };
}
