import "server-only";
import type {
  MediaItem,
  MediaKind,
  MediaLibrary,
  MediaSource,
  RunMediaFile,
} from "../media";
import { MAX_UPLOAD, mediaKindOf } from "../media";
import { isRecord } from "../runs";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";

// The local mock of docs/api/media.md: libraries, items and their bytes, in memory.
type StoredLibrary = Omit<
  MediaLibrary,
  "item_count" | "total_bytes" | "status_counts"
>;
type StoredItem = { item: MediaItem; bytes: Uint8Array };
type Store = {
  libraries: Map<string, StoredLibrary>;
  items: Map<string, StoredItem>;
};

const now = () => new Date().toISOString();
const TYPES: Record<string, string> = {
  mp4: "video/mp4",
  mov: "video/quicktime",
  m4v: "video/x-m4v",
  webm: "video/webm",
  mkv: "video/x-matroska",
  m4a: "audio/mp4",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  aac: "audio/aac",
  ogg: "audio/ogg",
  opus: "audio/opus",
  flac: "audio/flac",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  webp: "image/webp",
};

function newItem(
  library_id: string,
  name: string,
  kind: MediaKind,
  size: number,
  extra: Partial<MediaItem> = {},
): MediaItem {
  return {
    item_id: crypto.randomUUID(),
    library_id,
    name,
    kind,
    size_bytes: size,
    status: "ready",
    error: null,
    duration_seconds: null,
    width: null,
    height: null,
    orientation: null,
    description: "",
    tags: [],
    usable: true,
    source: { kind: "upload" },
    uploaded_by: null,
    created_at: now(),
    updated_at: now(),
    ...extra,
  };
}

function seed(): Store {
  const library_id = "11111111-1111-4111-8111-111111111111";
  const libraries = new Map<string, StoredLibrary>([
    [
      library_id,
      {
        library_id,
        name: "教练素材",
        description: "教练自己拍的健身房镜头",
        created_at: now(),
        updated_at: now(),
      },
    ],
  ]);
  const items = new Map<string, StoredItem>();
  const add = (item: MediaItem) =>
    items.set(item.item_id, {
      item,
      bytes: new TextEncoder().encode(item.name),
    });
  add(
    newItem(library_id, "卧推侧面.mp4", "video", 26_312_704, {
      duration_seconds: 12.4,
      width: 720,
      height: 1280,
      orientation: "portrait",
      description: "健身房杠铃卧推，侧面全景",
      tags: ["胸", "卧推"],
      uploaded_by: "王教练",
    }),
  );
  add(
    newItem(library_id, "配音0115.m4a", "audio", 812_000, {
      duration_seconds: 48.2,
      uploaded_by: "王教练",
    }),
  );
  return { libraries, items };
}

const globalStore = globalThis as typeof globalThis & {
  aepMockMedia?: Store;
};
const store: Store = (globalStore.aepMockMedia ??= seed());

function snapshot(library: StoredLibrary): MediaLibrary {
  const items = [...store.items.values()]
    .map((entry) => entry.item)
    .filter((item) => item.library_id === library.library_id);
  return {
    ...library,
    item_count: items.length,
    total_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0),
    status_counts: {
      processing: items.filter((item) => item.status === "processing").length,
      ready: items.filter((item) => item.status === "ready").length,
      error: items.filter((item) => item.status === "error").length,
    },
  };
}

function nameProblem(value: unknown, own?: string): Response | null {
  if (typeof value !== "string" || !value.trim() || value.trim().length > 50)
    return mockJson(
      { detail: [{ loc: ["body", "name"], msg: "名称要 1–50 个字符" }] },
      422,
    );
  const taken = [...store.libraries.values()].some(
    (library) => library.name === value.trim() && library.library_id !== own,
  );
  return taken ? mockError(`已存在名为「${value.trim()}」的媒体库`, 409) : null;
}

function tagsOf(value: unknown): string[] | null {
  if (
    !Array.isArray(value) ||
    value.length > 20 ||
    !value.every(
      (tag) => typeof tag === "string" && tag.trim() && tag.trim().length <= 20,
    )
  )
    return null;
  return [...new Set(value.map((tag: string) => tag.trim()))];
}

function runFiles(runId: string): RunMediaFile[] {
  return [...store.items.values()]
    .map((entry) => entry.item)
    .filter(
      (item) => item.source.kind === "run" && item.source.run_id === runId,
    )
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map((item) => ({
      item_id: item.item_id,
      library_id: item.library_id,
      name: item.name,
      kind: item.kind,
      size_bytes: item.size_bytes,
      duration_seconds: item.duration_seconds,
      width: item.width,
      height: item.height,
      download_path: `/media-libraries/${item.library_id}/items/${item.item_id}/file`,
    }));
}

async function upload(request: Request, library: StoredLibrary) {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return mockError("无法读取上传表单", 422);
  }
  const files = form
    .getAll("files")
    .filter((file): file is File => file instanceof File);
  if (!files.length)
    return mockError("请选择至少一个视频、音频或图片文件", 422);
  if (files.length > MAX_UPLOAD)
    return mockError("单次最多上传 20 个文件", 422);
  for (const file of files) {
    if (!mediaKindOf(file.name))
      return mockError(
        `文件「${file.name}」：只支持视频（mp4、mov、m4v、webm、mkv）、音频（m4a、mp3、wav、aac、ogg、opus、flac）和图片（jpg、jpeg、png、webp）`,
        422,
      );
    if (!file.size) return mockError(`文件「${file.name}」：是空文件`, 422);
  }
  const rawTags = form.get("tags");
  let tags: string[] = [];
  if (typeof rawTags === "string" && rawTags) {
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(rawTags);
    } catch {
      parsed = null;
    }
    const checked = tagsOf(parsed);
    if (!checked)
      return mockError(
        "「标签」要是 JSON 数组，最多 20 个，每个 1–20 个字",
        422,
      );
    tags = checked;
  }
  const description = form.get("description");
  const by = form.get("uploaded_by");
  const made: MediaItem[] = [];
  for (const file of files) {
    const item = newItem(
      library.library_id,
      file.name,
      mediaKindOf(file.name)!,
      file.size,
      {
        description: typeof description === "string" ? description : "",
        tags,
        uploaded_by: typeof by === "string" && by.trim() ? by.trim() : null,
      },
    );
    store.items.set(item.item_id, {
      item,
      bytes: new Uint8Array(await file.arrayBuffer()),
    });
    made.push(item);
  }
  return mockJson(made, 202);
}

export async function mockMedia(
  request: Request,
  path: string[],
): Promise<Response> {
  const method = request.method;
  if (path[0] === "runs") return mockJson(runFiles(path[1]));
  const [, id, section, itemId, action] = path;
  if (!id) {
    if (method === "GET")
      return mockJson(
        [...store.libraries.values()]
          .map(snapshot)
          .sort((a, b) => a.name.localeCompare(b.name)),
      );
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const problem = nameProblem(body.name);
    if (problem) return problem;
    const library: StoredLibrary = {
      library_id: crypto.randomUUID(),
      name: (body.name as string).trim(),
      description: typeof body.description === "string" ? body.description : "",
      created_at: now(),
      updated_at: now(),
    };
    store.libraries.set(library.library_id, library);
    return mockJson(snapshot(library), 201);
  }
  const library = store.libraries.get(id);
  if (!library) return mockError("没有这个媒体库", 404);
  if (!section) {
    if (method === "DELETE") {
      for (const [key, entry] of store.items)
        if (entry.item.library_id === id) store.items.delete(key);
      store.libraries.delete(id);
      return noContent();
    }
    if (method === "PATCH") {
      const body = await readBody(request);
      if (body instanceof Response) return body;
      if (body.name !== undefined) {
        const problem = nameProblem(body.name, id);
        if (problem) return problem;
        library.name = (body.name as string).trim();
      }
      if (typeof body.description === "string")
        library.description = body.description;
      library.updated_at = now();
    }
    return mockJson(snapshot(library));
  }
  if (!itemId) {
    if (method === "POST") return upload(request, library);
    const query = new URL(request.url).searchParams;
    const words = (query.get("q") ?? "")
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    const tag = query.get("tag");
    const kind = query.get("kind");
    const usable = query.get("usable");
    const limit = Number(query.get("limit") ?? 50);
    const offset = Number(query.get("offset") ?? 0);
    const found = [...store.items.values()]
      .map((entry) => entry.item)
      .filter((item) => item.library_id === id)
      .filter((item) => {
        const text = [item.name, item.description, ...item.tags]
          .join(" ")
          .toLowerCase();
        return words.every((word) => text.includes(word));
      })
      .filter((item) => !tag || item.tags.includes(tag))
      .filter((item) => !kind || item.kind === kind)
      .filter((item) => usable === null || String(item.usable) === usable)
      .reverse();
    return mockJson({
      items: found.slice(offset, offset + limit),
      total: found.length,
    });
  }
  const entry = store.items.get(itemId);
  if (!entry || entry.item.library_id !== id)
    return mockError("没有这个素材", 404);
  if (action === "file") {
    const ext = entry.item.name.split(".").pop()?.toLowerCase() ?? "";
    return new Response(entry.bytes.slice().buffer, {
      headers: {
        "Content-Type": TYPES[ext] ?? "application/octet-stream",
        "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(entry.item.name)}`,
        "Cache-Control": "no-store",
      },
    });
  }
  if (method === "DELETE") {
    store.items.delete(itemId);
    return noContent();
  }
  if (method === "PATCH") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const item = entry.item;
    if (body.tags !== undefined) {
      const tags = tagsOf(body.tags);
      if (!tags)
        return mockJson(
          {
            detail: [
              {
                loc: ["body", "tags"],
                msg: "「标签」最多 20 个，每个 1–20 个字",
              },
            ],
          },
          422,
        );
      item.tags = tags;
    }
    if (typeof body.description === "string") {
      if (body.description.length > 500)
        return mockJson(
          {
            detail: [
              { loc: ["body", "description"], msg: "说明最多 500 个字符" },
            ],
          },
          422,
        );
      item.description = body.description;
    }
    if (typeof body.usable === "boolean") item.usable = body.usable;
    if (isRecord(body.source)) item.source = body.source as MediaSource;
    item.updated_at = now();
  }
  return mockJson(entry.item);
}
