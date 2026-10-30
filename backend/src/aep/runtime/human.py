"""A person's decision inside a run: ask, then keep a record of the answer.

``ask`` pauses the run with LangGraph's ``interrupt`` and returns the answer when
the run is resumed (``POST /runs/{id}/resume``). Every app asks in the same shape,
so the workbench shows any of them: ``prompt``, ``options``, ``comment_required``
(the options that need a comment; the API refuses them without one) and the
app's ``details`` (tagged by ``details.type``); optionally ``option_labels``
(how to show each option). A ``form`` asks for ``fields`` to be filled in instead:
its one option is ``submit``, and the answer's ``values`` are checked against
the fields before the run goes on. ``record`` turns an answer into an
approval record (``step``, ``decision``, ``comment``, ``actor``, ``at``).

A flow app's human node may also say which option a changed answer or a remark
turns a decision into (``changed_branch``, always in the question; None when
unset) and mark what it shows with a fingerprint (``details.seen``), so a
decision made on an outdated page can be refused. A person may hang a remark on
one thing the page shows (a note on ``kind:id``); ``note_label`` finds that thing
in ``details.data`` and names it (docs/api/flow-apps.md, "人工节点在工作台上的样子").
"""

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from langgraph.types import interrupt


def ask(
    prompt: str,
    details: dict[str, Any] | None = None,
    options: Sequence[str] = ("approve", "reject"),
    comment_required: Sequence[str] = (),
    kind: str = "approval",
    option_labels: dict[str, str] | None = None,
    fields: list[dict[str, Any]] | None = None,
    changed_branch: str | None = None,
    seen: bool = False,
) -> dict[str, Any]:
    """Wait for a person; the answer has ``decision``, ``comment``, ``actor`` and, for a
    form or an approval with questions, ``values``. With ``seen``, ``details.seen`` is
    the fingerprint of the question."""
    question: dict[str, Any] = {
        "kind": kind,
        "prompt": prompt,
        "options": list(options),
        "comment_required": list(comment_required),
        "changed_branch": changed_branch,
    }
    if option_labels is not None:
        question["option_labels"] = option_labels
    if fields is not None:
        question["fields"] = fields
    if details is not None:
        question["details"] = details
        if seen:
            details["seen"] = fingerprint(question)
    return interrupt(question)


def fingerprint(question: dict[str, Any]) -> str:
    """``sha256:`` and the hex digest of what a person is shown: the question without
    ``details.seen`` and ``details.round``, as JSON with sorted keys, no spaces and
    text as it is."""
    details = {
        k: v for k, v in (question.get("details") or {}).items() if k not in ("seen", "round")
    }
    shown = {**question, "details": details} if "details" in question else question
    text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def record(step: str, answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": step,
        "decision": answer.get("decision"),
        "comment": answer.get("comment"),
        "actor": answer.get("actor"),
        "at": datetime.now(UTC).isoformat(),
    }


# What a note's ``kind`` is called in what is passed on (``feedback``); others are 条目.
NOTE_KINDS = {
    "metric": "指标",
    "dimension": "维度",
    "filter": "筛选",
    "rule": "规则",
    "question": "问题",
    "column": "列",
    "quality": "质量规则",
    "file": "文件",
}
NOTE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_NOTE_LABEL = 60
_NAMES = ("id", "key", "name", "path")
_LABELS = ("label", "name", "text", "title")


def note_label(data: Any, item: str) -> str | None:
    """The name of what ``item`` (``kind:id``) points at in ``data``, or None when it is
    not written that way or there is no such thing. The id is looked for in ``data``
    sorted by key, depth first: an object whose ``id``, ``key``, ``name`` or ``path`` is
    it (named by its ``label``, ``name``, ``text`` or ``title``); else an item of a list
    of texts (named by itself); else, for ``outer.inner``, an object whose ``id``,
    ``key`` or ``name`` is ``outer`` holding (at any depth) one whose is ``inner``
    (named by the id). A human node's record kept in ``data`` is not looked into."""
    kind, sep, wanted = item.partition(":")
    if not sep or not NOTE_KIND.match(kind) or not 1 <= len(wanted) <= 200:
        return None
    objects = list(_objects(data))
    label = None
    for obj in objects:
        if any(obj.get(k) == wanted for k in _NAMES):
            label = next(
                (v for k in _LABELS if isinstance(v := obj.get(k), str) and v.strip()), wanted
            )
            break
    else:
        if any(wanted in _texts(v) for v in _values(data)):
            label = wanted
        else:
            outer, _, inner = wanted.partition(".")
            if (
                outer
                and inner
                and any(
                    _named(obj, outer)
                    and any(_named(o, inner) for o in _objects(obj) if o is not obj)
                    for obj in objects
                )
            ):
                label = wanted
    if label is None:
        return None
    return label if len(label) <= MAX_NOTE_LABEL else label[: MAX_NOTE_LABEL - 1] + "…"


def _named(obj: dict[str, Any], name: str) -> bool:
    return any(obj.get(k) == name for k in ("id", "key", "name"))


def _is_record(value: Any) -> bool:
    return isinstance(value, dict) and all(k in value for k in ("node", "round", "decision"))


def _values(value: Any) -> Iterator[Any]:
    """``value`` and everything inside it, depth first, objects by key, records skipped."""
    if _is_record(value):
        return
    yield value
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _values(value[key])
    elif isinstance(value, list):
        for v in value:
            yield from _values(v)


def _objects(value: Any) -> Iterator[dict[str, Any]]:
    return (found for found in _values(value) if isinstance(found, dict))


def _texts(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
