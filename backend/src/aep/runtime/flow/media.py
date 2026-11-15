"""Media libraries in a flow app (docs/api/media.md, "应用用媒体库").

The configuration's ``media.libraries`` binds libraries to the app, each ``read`` or
``write``: what the app API (``/v1``) lets the app's keys see and change. A start
input of type ``media`` takes the id of an item in one of them; the start node puts out
the item's summary, with its file's path under ``AEP_MEDIA_DIR``.
"""

from collections.abc import Callable
from typing import Any, Literal

from aep.media.store import MediaItem, MediaStore

MEDIA = "media"
KINDS = ("video", "audio", "image")
KIND_LABELS = {"video": "视频", "audio": "音频", "image": "图片"}
ACCESS = ("read", "write")
MAX_LIBRARIES = 10

Access = Literal["read", "write"]
Add = Callable[[list[str | int], str], None]


def bindings(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The libraries the configuration binds, in its order; an unchecked configuration
    gives what of it is well formed."""
    media = config.get(MEDIA) if isinstance(config, dict) else None
    libraries = media.get("libraries") if isinstance(media, dict) else None
    if not isinstance(libraries, list):
        return []
    return [
        {"library_id": b["library_id"], "access": b["access"]}
        for b in libraries
        if isinstance(b, dict)
        and isinstance(b.get("library_id"), str)
        and b.get("access") in ACCESS
    ]


def access(config: dict[str, Any], library_id: str) -> Access | None:
    """How the app may use the library, or None when it does not bind it."""
    return next((b["access"] for b in bindings(config) if b["library_id"] == library_id), None)


async def check_binding(raw: Any, store: MediaStore | None, add: Add) -> dict[str, Any] | None:
    """The ``media`` setting as kept, or None when the configuration has none. Without a
    ``store`` only the shape is checked (an imported app may bind libraries this
    platform does not have)."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("libraries", []), list):
        add([MEDIA], 'media 要是 {"libraries": [...]}')
        return None
    libraries = raw.get("libraries", [])
    if len(libraries) > MAX_LIBRARIES:
        add([MEDIA, "libraries"], f"最多绑定 {MAX_LIBRARIES} 个媒体库")
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for i, entry in enumerate(libraries):
        loc: list[str | int] = [MEDIA, "libraries", i]
        if not isinstance(entry, dict):
            add(loc, '每一项要是 {"library_id", "access"}')
            continue
        library_id = entry.get("library_id")
        if not isinstance(library_id, str) or not library_id:
            add([*loc, "library_id"], "要选一个媒体库")
            continue
        if entry.get("access") not in ACCESS:
            add([*loc, "access"], "权限只能是 read（只读）或 write（可以上传和修改）")
        if library_id in seen:
            add([*loc, "library_id"], f"媒体库 {library_id} 绑定了两次")
        elif store is not None and await store.library(library_id) is None:
            add([*loc, "library_id"], "没有这个媒体库")
        seen.add(library_id)
        kept.append({"library_id": library_id, "access": entry.get("access")})
    return {"libraries": kept}


def check_inputs(start: dict[str, Any], bound: list[dict[str, Any]], add: Add) -> None:
    """A start node's media inputs name libraries the app binds."""
    ids = {b["library_id"] for b in bound}
    inputs = start.get("inputs")
    if not isinstance(inputs, list):
        return  # reported by the start node's own checks
    for i, f in enumerate(inputs):
        if not isinstance(f, dict):
            continue
        library = f.get("library")
        if f.get("type") == MEDIA and isinstance(library, str) and library and library not in ids:
            where: list[str | int] = ["graph", "nodes", start["id"], "inputs", i, "library"]
            add(where, "素材输入要选一个绑定到应用的媒体库")


def field_problems(f: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str]]:
    """What is wrong with a media input's own settings (whether its library is bound is
    checked with the whole configuration)."""
    found: list[tuple[tuple[str | int, ...], str]] = []
    library = f.get("library")
    if not isinstance(library, str) or not library:
        found.append((("library",), "素材输入要选一个绑定到应用的媒体库"))
    if "kinds" in f:
        kinds = f["kinds"]
        if (
            not isinstance(kinds, list)
            or not 1 <= len(kinds) <= len(KINDS)
            or len(set(map(str, kinds))) != len(kinds)
            or any(k not in KINDS for k in kinds)
        ):
            found.append((("kinds",), "素材类型只能是 video、audio、image"))
    if f.get("default") is not None:
        found.append((("default",), "素材输入不能设默认值"))
    return found


async def value_problems(
    store: MediaStore | None, fields: list[dict[str, Any]], values: dict[str, Any]
) -> dict[str, str]:
    """Why the items given for media inputs cannot be used, by input key (only those
    whose value is text: the rest are told by ``nodes.field_problem``)."""
    problems: dict[str, str] = {}
    for f in fields:
        value = values.get(f["key"])
        if f.get("type") != MEDIA or not isinstance(value, str) or not value.strip():
            continue
        problem = await item_problem(store, f, value)
        if problem is not None:
            problems[f["key"]] = problem
    return problems


async def item_problem(store: MediaStore | None, f: dict[str, Any], item_id: str) -> str | None:
    label = f["label"]
    library = await store.library(f["library"]) if store is not None else None
    item = await store.item(f["library"], item_id) if library is not None and store else None
    if item is None:
        name = library.name if library is not None else f["library"]
        return f"{label}：媒体库「{name}」里没有这个素材"
    kinds = f.get("kinds") or list(KINDS)
    if item.kind not in kinds:
        return f"{label}要是" + "或".join(KIND_LABELS[k] for k in kinds)
    if item.status == "processing":
        return f"{label}：素材还在处理，稍后再试"
    if item.status == "error":
        return f"{label}：素材处理失败了，不能用"
    return None


async def names(store: MediaStore, config: dict[str, Any]) -> dict[str, str]:
    """The names of the libraries the configuration binds, by id (those that exist)."""
    found: dict[str, str] = {}
    for bound in bindings(config):
        library = await store.library(bound["library_id"])
        if library is not None:
            found[library.library_id] = library.name
    return found


def required(config: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    """The ``media_libraries`` of a file's ``requires`` and a package's requirements."""
    return [
        {"library_id": b["library_id"], "name": names.get(b["library_id"]), "access": b["access"]}
        for b in bindings(config)
    ]


def summary(item: MediaItem) -> dict[str, Any]:
    """What the start node puts out for a media input."""
    return {
        "item_id": item.item_id,
        "library_id": item.library_id,
        "name": item.name,
        "kind": item.kind,
        "duration_seconds": item.duration_seconds,
        "width": item.width,
        "height": item.height,
        "path": item.rel_path,
    }
