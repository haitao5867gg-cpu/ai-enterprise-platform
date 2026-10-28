"""A person's decision inside a run: ask, then keep a record of the answer.

``ask`` pauses the run with LangGraph's ``interrupt`` and returns the answer when
the run is resumed (``POST /runs/{id}/resume``). Every app asks in the same shape,
so the workbench shows any of them: ``prompt``, ``options``, ``comment_required``
(the options that need a comment; the API refuses them without one) and the
app's ``details`` (tagged by ``details.type``). ``record`` turns an answer into an
approval record (``step``, ``decision``, ``comment``, ``actor``, ``at``).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from langgraph.types import interrupt


def ask(
    prompt: str,
    details: dict[str, Any] | None = None,
    options: Sequence[str] = ("approve", "reject"),
    comment_required: Sequence[str] = (),
) -> dict[str, Any]:
    """Wait for a person; the answer has ``decision``, ``comment`` and ``actor``."""
    question: dict[str, Any] = {
        "kind": "approval",
        "prompt": prompt,
        "options": list(options),
        "comment_required": list(comment_required),
    }
    if details is not None:
        question["details"] = details
    return interrupt(question)


def record(step: str, answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": step,
        "decision": answer.get("decision"),
        "comment": answer.get("comment"),
        "actor": answer.get("actor"),
        "at": datetime.now(UTC).isoformat(),
    }
