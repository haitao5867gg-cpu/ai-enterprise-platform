"""How an app is used (docs/api/ops.md, "应用的使用情况"): its runs by the way they came
in, its conversations, how often its agent found grounds for an answer, the feedback its
runs got, the tools its agents called and the apps it called. The run metrics and
daily figures are the operations dashboard's (``aep.ops``), for the one app.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from aep import entries as ways
from aep.conversations import chat_settings
from aep.feedback import FeedbackStore
from aep.ops import Ops
from aep.runtime.agent_app import KIND as AGENT
from aep.runtime.flow import graph as flowgraph
from aep.runtime.service import RunService
from aep.runtime.store import RunRecord, counted

LIMIT = 2000  # runs looked at, the newest first
TOP = 20
NOT_A_TOOL = "read_skill"  # reading a Skill is not using a tool


def _agents(record: RunRecord) -> list[str]:
    """The agent nodes of the configuration the run went with."""
    config = record.config or {}
    if "graph" not in config:
        return []
    return [n["id"] for n in flowgraph.nodes(config) if n["type"] == "agent"]


async def app_usage(
    app: str, days: int, service: RunService, ops: Ops, feedback: FeedbackStore
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    records = await service.of_app(app, since, LIMIT + 1)
    truncated = len(records) > LIMIT
    # As on the dashboard, a draft's trial and evaluation runs (debug chats too) do not count.
    records = [r for r in records[:LIMIT] if counted(r)]
    ids = {r.run_id for r in records}
    found = await ops.app_metrics(records, days)

    came = Counter(ways.of(r) for r in records)
    kind = await service.app_kind(app)
    converses = kind == AGENT
    if kind == "flow":
        try:
            converses = chat_settings(await service.conversable(app)) is not None
        except Exception:  # noqa: BLE001 - an app that cannot converse (now)
            converses = False
    conversations = None
    if converses:
        turns = Counter(r.conversation_id for r in records if r.conversation_id)
        opened = {r.conversation_id for r in records if r.conversation_turn == 1}
        counts = [turns[c] for c in opened]
        conversations = {
            "count": len(opened),
            "turns_avg": round(sum(counts) / len(counts), 2) if counts else None,
        }

    answered = ungrounded = 0
    tools: dict[tuple[str, str | None, str | None], list[int]] = {}
    for record in records:
        agents = _agents(record)
        if not agents or record.cleared_at is not None or record.status not in ("done", "error"):
            continue
        values = await service.values(record)
        if kind == AGENT and record.status == "done":
            grounded = (values.get("result") or {}).get("grounded")
            answered += grounded is True
            ungrounded += grounded is False
        for node in agents:
            for step in ((values.get("vars") or {}).get(node) or {}).get("steps") or []:
                name = step.get("tool")
                if not isinstance(name, str) or name == NOT_A_TOOL:
                    continue
                key = (name, step.get("datasource"), step.get("app"))
                seen = tools.setdefault(key, [0, 0])
                seen[0] += 1
                seen[1] += bool(step.get("error"))

    called: dict[str, list[int]] = {}
    for record in await service.called_since(since):
        if str((record.trigger or {}).get("run_id")) in ids:
            seen = called.setdefault(record.app, [0, 0])
            seen[0] += 1
            seen[1] += record.status == "error"
    titles = await service.app_titles()
    given = await feedback.for_runs(list(ids))
    return {
        "days": days,
        **found,
        "entries": {way: came.get(way, 0) for way in ways.RUN_ENTRIES},
        "conversations": conversations,
        "answers": {"answered": answered, "ungrounded": ungrounded} if kind == AGENT else None,
        "feedback": {
            "good": sum(f.rating == "good" for f in given),
            "bad": sum(f.rating == "bad" for f in given),
        },
        "tools": [
            {"tool": t, "datasource": d, "app": a, "calls": n, "errors": e}
            for (t, d, a), (n, e) in sorted(tools.items(), key=lambda i: (-i[1][0], i[0][0]))
        ][:TOP],
        "apps": [
            {"app": a, "title": titles.get(a, a), "calls": n, "errors": e}
            for a, (n, e) in sorted(called.items(), key=lambda i: (-i[1][0], i[0]))
        ][:TOP],
        "truncated": truncated,
    }
