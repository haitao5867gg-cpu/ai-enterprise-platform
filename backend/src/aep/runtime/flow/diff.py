"""What changed between two versions of something a person reviews (docs/api/flow-apps.md,
人工节点的 ``changes``): items of lists matched by a key, each added, removed or changed,
with the fields that changed in a few words (jadeflow 4th retest I-3: after a comment the
model also changed checks nobody asked about, and the person had to compare by hand).
"""

import re
from typing import Any

MAX_CHANGES = 50
PATH = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
FIELD = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def normalize(value: Any) -> Any:
    """A value as compared across versions: text with its spaces collapsed, objects without
    empty fields (null, "", [], {}), keys sorted."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items()) if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def short(text: str, most: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 1] + "…"


def field_changes(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Which fields changed, in one short phrase: ``where、tolerance（0 → 0.005）``; a value
    is shown only when both sides are short and plain."""
    parts = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if all(isinstance(v, int | float | str | None) for v in (old, new)):
            old_text = "无" if old is None else str(old)
            new_text = "无" if new is None else str(new)
            if len(old_text) + len(new_text) <= 40:
                parts.append(f"{key}（{old_text} → {new_text}）")
                continue
        parts.append(key)
    return "、".join(parts)


def at_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def items(value: Any, lists: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What the lists of ``value`` hold, as kept between rounds: by list path, by item key,
    ``{"label": ..., "fields": <normalized item>}``. Items without a key are left out; of
    two with the same key the first counts."""
    out: dict[str, dict[str, Any]] = {}
    for spec in lists:
        found: dict[str, Any] = {}
        listed = at_path(value, spec["path"])
        for item in listed if isinstance(listed, list) else []:
            if not isinstance(item, dict):
                continue
            key = item.get(spec["key"])
            if not isinstance(key, str | int) or isinstance(key, bool) or str(key) in found:
                continue
            label = item.get(spec.get("label") or spec["key"])
            found[str(key)] = {
                "label": str(label) if isinstance(label, str | int | float) else str(key),
                "fields": normalize(item),
            }
        out[spec["path"]] = found
    return out


def _mentioned(key: str, label: str, feedback: dict[str, Any]) -> bool:
    """Whether a person's comment or notes named the item: a note hung on it (``check:k``),
    or its key or name in what they wrote."""
    words = [w for w in (key, label) if len(w) >= 2]
    texts = [str(feedback.get("comment") or "")]
    for note in feedback.get("notes") or []:
        if not isinstance(note, dict):
            continue
        if str(note.get("item") or "").partition(":")[2] == key:
            return True
        texts += [str(note.get("label") or ""), str(note.get("text") or "")]
    return any(w in t for w in words for t in texts)


def changes(
    before: dict[str, dict[str, Any]] | None,
    now: dict[str, dict[str, Any]],
    lists: list[dict[str, Any]],
    feedback: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """What changed from ``before`` to ``now`` (both from ``items``), list by list in the
    order of ``lists``: ``{list, kind, key, label, fields, mentioned}``. ``kind`` is added,
    removed or changed; ``fields`` says what changed (changed only, else ""). With the
    ``feedback`` that sent the work back (the person's comment and notes), ``mentioned``
    says whether they named the item; without it, null. None ``before``: nothing to
    compare with, []. At most ``MAX_CHANGES``."""
    if before is None:
        return []
    out: list[dict[str, Any]] = []
    for spec in lists:
        path = spec["path"]
        old, new = before.get(path) or {}, now.get(path) or {}
        found = []
        for key, item in new.items():
            if key not in old:
                found.append(("added", key, item["label"], ""))
            elif item["fields"] != old[key]["fields"]:
                said = field_changes(old[key]["fields"], item["fields"])
                found.append(("changed", key, item["label"], said))
        found += [
            ("removed", key, item["label"], "") for key, item in old.items() if key not in new
        ]
        for kind, key, label, said in found:
            out.append(
                {
                    "list": spec.get("name") or path,
                    "kind": kind,
                    "key": key,
                    "label": label,
                    "fields": said,
                    "mentioned": None if feedback is None else _mentioned(key, label, feedback),
                }
            )
    return out[:MAX_CHANGES]


def check(value: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """What is wrong with a human node's ``changes`` setting, located inside it."""
    from aep.runtime.flow import refs

    if not isinstance(value, dict):
        return [((), "changes 要写成 {of, lists}")]
    found: list[tuple[tuple[str | int, ...], str]] = []
    of = value.get("of")
    if not isinstance(of, str) or refs.whole(of) is None:
        found.append((("of",), "要比较的内容 of 必须恰好是一个变量，如 {{check_plan.plan}}"))
    lists = value.get("lists")
    if not isinstance(lists, list) or not 1 <= len(lists) <= 10:
        return found + [(("lists",), "要比较的列表 lists 要有 1–10 个")]
    paths: set[str] = set()
    for i, spec in enumerate(lists):
        if not isinstance(spec, dict):
            found.append((("lists", i), "每个列表要写成 {path, key, label, name}"))
            continue
        path = spec.get("path")
        if not isinstance(path, str) or not PATH.match(path):
            found.append(
                (("lists", i, "path"), "path 是列表在内容里的位置，如 checks、brief.metrics")
            )
        elif path in paths:
            found.append((("lists", i, "path"), f"列表 {path} 重复"))
        else:
            paths.add(path)
        for key, required in (("key", True), ("label", False)):
            name = spec.get(key)
            if (name is not None or required) and (
                not isinstance(name, str) or not FIELD.match(name)
            ):
                found.append(((("lists", i, key)), f"{key} 是条目里的字段名，如 id、label"))
        name = spec.get("name")
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 20):
            found.append((("lists", i, "name"), "name 是给人看的名字，1–20 个字符"))
    for key in value:
        if key not in ("of", "lists"):
            found.append(((key,), f"changes 没有 {key} 这一项"))
    return found
