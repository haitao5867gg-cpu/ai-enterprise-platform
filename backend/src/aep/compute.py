"""Databricks time the platform's runs spend, reported by aep.databricks and kept by the
operations dashboard (aep.ops, docs/api/ops.md)."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aep import llm

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComputeRecord:
    run_id: str | None
    node: str | None
    kind: str  # "job" (a Jobs API run) | "command" (code run on a cluster)
    cluster_id: str | None
    seconds: float


ComputeRecorder = Callable[[ComputeRecord], Awaitable[None]]
_recorder: ComputeRecorder | None = None


def set_recorder(recorder: ComputeRecorder | None) -> None:
    """Where compute usage goes; None records nothing (tests, scripts)."""
    global _recorder
    _recorder = recorder


async def record(kind: str, cluster_id: str | None, seconds: float) -> None:
    """Records Databricks time spent for the current run step; never fails the caller."""
    if _recorder is None or seconds <= 0:
        return
    run_id, node = llm.run_context()
    try:
        await _recorder(ComputeRecord(run_id, node, kind, cluster_id, seconds))
    except Exception:  # noqa: BLE001 - bookkeeping must not break the run
        log.warning("could not record compute usage", exc_info=True)
