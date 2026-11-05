"""What changed between two versions of something a person reviews (docs/api/flow-apps.md,
人工节点的 ``changes``): items of lists matched by a key, each added, removed or changed,
with the fields that changed in a few words (jadeflow 4th retest I-3: after a comment the
model also changed checks nobody asked about, and the person had to compare by hand).
"""

import re
from difflib import SequenceMatcher
from typing import Any

MAX_CHANGES = 50
MAX_IGNORE = 10
PATH = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
FIELD = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
# How a person names an item in a comment besides its key or whole name (jadeflow 5th
# retest I-3: every change was marked as not asked for, the ones they asked for too): a
# quoted phrase found in the item, a run of characters shared with its name, or a word
# that is a part of its key (Actual → chk_actual_months_null).
QUOTED = re.compile(r"[「『“\"'`‘]([^「」『』“”\"'`‘’\n]{2,80})[」』”\"'`’]")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
COMMON_WORDS = {
    "null", "none", "true", "false", "check", "checks", "rule", "rules", "error", "warn",
    "info", "table", "where", "select", "from", "plan", "code", "data",
}  # fmt: skip
MIN_SHARED = 5
# Two items of a list, one gone and one new, are one item reworded when their names are
# at least this alike (jadeflow 5th retest I-3: a rule with a few words changed was listed
# as one removed and one added).
MIN_ALIKE = 0.6


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


def bare(text: str) -> str:
    """Text compared loosely: lower case, without spaces and punctuation."""
    return re.sub(r"[\W_]+", "", text.lower())


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [str(value)] if isinstance(value, int | float) and not isinstance(value, bool) else []


def _shared(a: str, b: str) -> int:
    """How long the longest run of characters is that ``a`` and ``b`` share."""
    return SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b)).size


def _named(key: str, label: str, fields: list[Any], feedback: dict[str, Any]) -> bool:
    """Whether a person's comment or notes named the item (any of its versions, ``fields``):
    a note hung on it (``check:k``), its key or name, a quoted phrase found in it, a run of
    ``MIN_SHARED`` characters shared with its name, or a word that is a part of its key."""
    words = [w for w in (key, label) if len(w) >= 2]
    texts = [str(feedback.get("comment") or "")]
    for note in feedback.get("notes") or []:
        if not isinstance(note, dict):
            continue
        if str(note.get("item") or "").partition(":")[2] == key:
            return True
        texts += [str(note.get("label") or ""), str(note.get("text") or "")]
    if any(w in t for w in words for t in texts):
        return True
    said = "\n".join(texts)
    held = bare(" ".join([key, label, *(s for f in fields for s in _strings(f))]))
    if any(len(q) >= 2 and q in held for q in map(bare, QUOTED.findall(said))):
        return True
    name = bare(label)
    if len(name) >= MIN_SHARED and _shared(bare(said), name) >= MIN_SHARED:
        return True
    parts = {p for p in re.split(r"[^a-z0-9]+", key.lower()) if p}
    for word in WORD.findall(said):
        low = word.lower()
        if low not in COMMON_WORDS and (low in parts or ("_" in low and low in key.lower())):
            return True
    return False


def _answered(key: str, label: str, fields: list[Any], feedback: dict[str, Any]) -> bool:
    """Whether the item follows from a question the person answered: the question is in it
    (a confirmed rule says ``已确认：<question>——<answer>``)."""
    held = bare(" ".join([key, label, *(s for f in fields for s in _strings(f))]))
    for answer in feedback.get("answers") or []:
        asked = bare(str(answer.get("label") or "")) if isinstance(answer, dict) else ""
        if len(asked) >= 2 and asked in held:
            return True
    return False


def _because(key: str, label: str, fields: list[Any], feedback: dict[str, Any] | None) -> Any:
    if feedback is None:
        return None
    if _named(key, label, fields, feedback):
        return "remarks"
    return "answer" if _answered(key, label, fields, feedback) else False


def _pairs(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Items gone from ``old`` and new in ``new`` that are one item reworded: the most
    alike first, each used once, at least ``MIN_ALIKE`` alike by name."""
    gone = [k for k in old if k not in new]
    came = [k for k in new if k not in old]
    scored = sorted(
        (
            (SequenceMatcher(None, bare(old[g]["label"]), bare(new[c]["label"])).ratio(), g, c)
            for g in gone
            for c in came
        ),
        key=lambda t: -t[0],
    )
    used: set[str] = set()
    pairs = []
    for ratio, g, c in scored:
        if ratio >= MIN_ALIKE and g not in used and c not in used:
            used |= {g, c}
            pairs.append((g, c))
    return pairs


def _changed(was: dict[str, Any], now: dict[str, Any], spec: dict[str, Any], reworded: bool) -> str:
    """What changed in an item, without the list's ``ignore`` fields ("" when nothing did).
    A reworded item (paired by ``_pairs``) starts with 改了文字 when its name changed, and
    its key and name fields are not listed again."""
    ignore = set(spec.get("ignore") or [])
    if reworded:
        ignore |= {spec["key"], spec.get("label") or spec["key"]}
    rest = field_changes(
        {k: v for k, v in was["fields"].items() if k not in ignore},
        {k: v for k, v in now["fields"].items() if k not in ignore},
    )
    if not reworded:
        return rest
    if was["label"] != now["label"]:
        return "、".join(p for p in ("改了文字", rest) if p)
    # Only the key changed (a check given another id): say so.
    return "、".join(
        p for p in (f"{spec['key']}（{_key(was, spec)} → {_key(now, spec)}）", rest) if p
    )


def _key(item: dict[str, Any], spec: dict[str, Any]) -> str:
    return short(str(item["fields"].get(spec["key"], "")), 30)


def changes(
    before: dict[str, dict[str, Any]] | None,
    now: dict[str, dict[str, Any]],
    lists: list[dict[str, Any]],
    feedback: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """What changed from ``before`` to ``now`` (both from ``items``), list by list in the
    order of ``lists``: ``{list, kind, key, label, fields, mentioned, because}``. ``kind``
    is added, removed or changed (an item gone and a new one alike by name are one item
    changed, ``fields`` starting with 改了文字); ``fields`` says what changed (changed only,
    else ""), leaving out the list's ``ignore`` fields. With the ``feedback`` that sent the
    work back (the person's comment, notes and answers), ``because`` says why the item
    changed as far as the person is concerned: ``remarks`` (they named it), ``answer``
    (it follows from a question they answered) or false; ``mentioned`` is whether it is
    either. Without ``feedback`` both are null. None ``before``: nothing to compare with,
    []. At most ``MAX_CHANGES``."""
    if before is None:
        return []
    out: list[dict[str, Any]] = []
    for spec in lists:
        old, new = before.get(spec["path"]) or {}, now.get(spec["path"]) or {}
        paired = dict(_pairs(old, new))
        twin = {c: g for g, c in paired.items()}
        found = []
        for key, item in new.items():
            was = old.get(twin.get(key, key))
            if was is None:
                found.append(("added", key, item["label"], "", [item["fields"]]))
            elif said := _changed(was, item, spec, key in twin):
                found.append(("changed", key, item["label"], said, [was["fields"], item["fields"]]))
        found += [
            ("removed", key, item["label"], "", [item["fields"]])
            for key, item in old.items()
            if key not in new and key not in paired
        ]
        for kind, key, label, said, versions in found:
            because = _because(key, label, versions, feedback)
            out.append(
                {
                    "list": spec.get("name") or spec["path"],
                    "kind": kind,
                    "key": key,
                    "label": label,
                    "fields": said,
                    "mentioned": None if because is None else because is not False,
                    "because": because or None,
                }
            )
    return out[:MAX_CHANGES]


def check_lists(lists: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """What is wrong with the lists to compare (``lists`` of a ``changes`` setting)."""
    if not isinstance(lists, list) or not 1 <= len(lists) <= 10:
        return [(("lists",), "要比较的列表 lists 要有 1–10 个")]
    found: list[tuple[tuple[str | int, ...], str]] = []
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
        ignore = spec.get("ignore")
        if ignore is not None and (
            not isinstance(ignore, list)
            or len(ignore) > MAX_IGNORE
            or not all(isinstance(f, str) and FIELD.match(f) for f in ignore)
        ):
            found.append(
                (("lists", i, "ignore"), f"ignore 是比较时不算的字段名，最多 {MAX_IGNORE} 个")
            )
        elif ignore and spec.get("key") in ignore:
            found.append((("lists", i, "ignore"), "配对用的 key 不能不算"))
    return found


def check(value: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """What is wrong with a human node's ``changes`` setting, located inside it."""
    from aep.runtime.flow import refs

    if not isinstance(value, dict):
        return [((), "changes 要写成 {of, lists}")]
    found: list[tuple[tuple[str | int, ...], str]] = []
    of = value.get("of")
    if not isinstance(of, str) or refs.whole(of) is None:
        found.append((("of",), "要比较的内容 of 必须恰好是一个变量，如 {{check_plan.plan}}"))
    found += check_lists(value.get("lists"))
    for key in value:
        if key not in ("of", "lists"):
            found.append(((key,), f"changes 没有 {key} 这一项"))
    return found
