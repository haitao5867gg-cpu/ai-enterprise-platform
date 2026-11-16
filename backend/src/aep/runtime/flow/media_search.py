"""The 检索素材 node (``media_search``): usable items of a library the app binds, with
their descriptions, for the nodes after it (docs/api/media-search.md). Whether the app
binds the library is checked with the whole configuration (aep.runtime.flow.check)."""

from typing import Any

from aep.media.store import ItemQuery, MediaItem
from aep.runtime.flow import media as flowmedia
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    INPUT_ERROR,
    TYPES,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
)

TYPE = "media_search"
MAX_QUERY = 200
LIMIT = (1, 200)
DEFAULT_LIMIT = 50
MAX_TAGS = 20
MAX_TAG = 20
NOT_BOUND = "要选一个绑定到应用的媒体库"
KINDS_PROBLEM = "素材类型只能是 video、audio、image"


def _kinds_ok(kinds: Any) -> bool:
    return (
        isinstance(kinds, list)
        and 1 <= len(kinds) <= len(flowmedia.KINDS)
        and len(set(map(str, kinds))) == len(kinds)
        and all(k in flowmedia.KINDS for k in kinds)
    )


def _tags_ok(tags: Any) -> bool:
    return (
        isinstance(tags, list)
        and len(tags) <= MAX_TAGS
        and all(isinstance(t, str) and 1 <= len(t.strip()) <= MAX_TAG for t in tags)
    )


async def check_media_search(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    library = node.get("library")
    if not isinstance(library, str) or not library:
        p.add(("library",), NOT_BOUND)
    query = node.get("query", "")
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        p.add(("query",), f"搜索词最多 {MAX_QUERY} 个字符")
    if "kinds" in node and not _kinds_ok(node["kinds"]):
        p.add(("kinds",), KINDS_PROBLEM)
    if "tags" in node and not _tags_ok(node["tags"]):
        p.add(("tags",), f"标签最多 {MAX_TAGS} 个，每个 1–{MAX_TAG} 个字")
    limit = node.get("limit", DEFAULT_LIMIT)
    low, high = LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or not low <= limit <= high:
        p.add(("limit",), f"最多取几个要在 {low}–{high} 之间")


def check_bound(
    node: dict[str, Any], bound: list[dict[str, Any]], add: flowmedia.Add, where: list[str | int]
) -> None:
    """The node searches a library the app binds (for reading or writing)."""
    library = node.get("library")
    if not isinstance(library, str) or not library:
        return  # told by the node's own check
    if not any(b["library_id"] == library for b in bound):
        add([*where, "library"], NOT_BOUND)


def found(item: MediaItem) -> dict[str, Any]:
    """An item as the node puts it out: its summary, as a start input's, and what a model
    picking footage needs to know about it."""
    width, height = item.width, item.height
    orientation = None
    if width and height:
        orientation = "square" if width == height else "landscape" if width > height else "portrait"
    return {
        **flowmedia.summary(item),
        "orientation": orientation,
        "description": item.description,
        "tags": list(item.tags),
        "source": dict(item.source),
    }


async def run_media_search(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    library_id = node.get("library")
    store = rt.media
    library = (
        await store.library(library_id)
        if store is not None and isinstance(library_id, str) and library_id
        else None
    )
    if library is None:
        raise NodeFailure(INPUT_ERROR, f"媒体库不见了：{refs.as_text(library_id)}")
    assert store is not None
    text = refs.render(str(node.get("query") or ""), rt.lookup(state)).strip()
    kinds = node.get("kinds")
    query = ItemQuery(
        q=text[:MAX_QUERY],
        kinds=tuple(kinds) if kinds else None,
        tags=tuple(dict.fromkeys(t.strip() for t in node.get("tags") or [])),
        usable=True,
        status="ready",
        limit=node.get("limit", DEFAULT_LIMIT),
    )
    items, total = await store.items(library.library_id, query)
    listed = [found(item) for item in items]
    return Outcome({"items": listed, "count": len(listed), "total": total})


def _produces(node: dict[str, Any]) -> list[str]:
    return ["items", "count", "total"]


MEDIA_SEARCH_TYPES: dict[str, NodeType] = {
    TYPE: NodeType(
        TYPE,
        "检索素材",
        "按词、类型和标签从绑定的媒体库里取出能用的素材，带画面描述",
        "tool",
        "tool",
        check_media_search,
        run_media_search,
        lambda node: None,
        _produces,
        outputs=(
            ("items", "找到的素材：摘要、画面描述、标签和来源"),
            ("count", "找到几个"),
            ("total", "符合条件的一共几个"),
        ),
        defaults={
            "library": "",
            "query": "",
            "kinds": ["video", "image"],
            "tags": [],
            "limit": DEFAULT_LIMIT,
        },
        error_handling=True,
    )
}

TYPES.update(MEDIA_SEARCH_TYPES)
