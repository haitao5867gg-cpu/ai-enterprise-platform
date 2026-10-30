"""Testing a connector before it is registered, and its health once it is.

``ConnectorCheck.test`` is the 「测试连接」 of docs/api/datasources.md: the address
rules (``aep.connector_urls``), then a timed listing of the connector's tools, and
for a data source the ETL tools it must offer. ``health`` lists the tools of a
registered source, kept for a minute per source and address. People only ever see
the kind of problem and its Chinese text; the error itself goes to the log.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aep.config import Settings
from aep.connector_urls import TEXT as ADDRESS_TEXT
from aep.connector_urls import Rejected, check_url, mask_url
from aep.datasources import DATABASE_KINDS, ETL_TOOLS, DataSource
from aep.tools.mcp import ConnectionProblem, ConnectorUnavailable, McpTools, ToolError, ToolInfo

log = logging.getLogger(__name__)

HEALTH_TTL_SECONDS = 60.0
# The Chinese text of each problem (docs/api/datasources.md, 问题分类).
PROBLEM_TEXT = {
    **ADDRESS_TEXT,
    "timeout": "超时：连接器在 {seconds} 秒内没有响应。请确认连接器已经启动，地址和端口没写错。",
    "refused": "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。",
    "not_mcp": (
        "不是 MCP 服务：能连上这个地址，但对方不是 MCP 连接器。请检查路径（一般以 /mcp 结尾）；"
        "如果确实是连接器，请让管理员在连接器上允许这个主机名。"
    ),
    "other": (
        "网络不通：找不到这个主机，或者网络不通，也可能是 http 和 https 写反了。"
        "请检查地址，必要时请管理员查看平台日志。"
    ),
    "missing_tools": (
        "缺少 ETL 需要的工具：{tools}。数据源要提供 " + "、".join(ETL_TOOLS) + " 这 6 个工具；"
        "如果它只提供别的工具，请把类型选成「工具服务」。"
    ),
    "no_tools": "连接器没有提供任何工具，工具服务至少要有一个工具。",
}
TOOLS_FAILED = "连接器列工具时报错，请让管理员查看连接器的日志。"


@dataclass(frozen=True)
class Problem:
    code: str
    message: str


@dataclass(frozen=True)
class TestResult:
    ok: bool
    latency_ms: int | None
    tools: list[ToolInfo]
    missing_tools: list[str]
    problems: list[Problem]
    # The normalised address, for saving; None when the address was rejected.
    url: str | None = None


@dataclass(frozen=True)
class Health:
    ok: bool
    latency_ms: int | None
    tool_count: int | None
    error: ConnectionProblem | None
    message: str | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def connection_problem(reason: ConnectionProblem, seconds: float) -> Problem:
    return Problem(reason, PROBLEM_TEXT[reason].format(seconds=f"{seconds:g}"))


def failed_detail(result: TestResult) -> str:
    """Why saving was refused: every problem, each a sentence of its own."""
    return "测试连接没有通过。" + "".join(p.message for p in result.problems)


def _failed(problem: Problem) -> TestResult:
    return TestResult(False, None, [], [], [problem])


def _log_unavailable(url: str, exc: ConnectorUnavailable) -> None:
    cause = exc.__cause__
    log.info("connector %s unavailable (%s): %r", mask_url(url), exc.reason, cause or exc)


class ConnectorCheck:
    """Tests connectors and keeps each source's health for ``ttl`` seconds, per process."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl: float = HEALTH_TTL_SECONDS
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._health: dict[str, tuple[str, float, Health]] = {}

    async def test(self, tools: McpTools, kind: str, url: str, s: Settings) -> TestResult:
        """Whether a connector at ``url`` may be registered as ``kind``; an address the
        rules reject is never connected to."""
        try:
            normalised = check_url(url, s)
        except Rejected as exc:
            return _failed(Problem(exc.code, exc.message))
        started = time.perf_counter()
        try:
            listed = await tools.list_tools(normalised, timeout_seconds=s.connector_check_seconds)
        except ConnectorUnavailable as exc:
            _log_unavailable(normalised, exc)
            return _failed(connection_problem(exc.reason, s.connector_check_seconds))
        except ToolError as exc:
            log.info("connector %s failed listing its tools: %r", mask_url(normalised), exc)
            return _failed(connection_problem("not_mcp", s.connector_check_seconds))
        latency = round((time.perf_counter() - started) * 1000)
        names = {t.name for t in listed}
        problems = []
        missing = [t for t in ETL_TOOLS if t not in names] if kind in DATABASE_KINDS else []
        if missing:
            text = PROBLEM_TEXT["missing_tools"].format(tools="、".join(missing))
            problems.append(Problem("missing_tools", text))
        if kind not in DATABASE_KINDS and not listed:
            problems.append(Problem("no_tools", PROBLEM_TEXT["no_tools"]))
        return TestResult(not problems, latency, listed, missing, problems, normalised)

    async def health(
        self, tools: McpTools, source: DataSource, s: Settings, fresh: bool = False
    ) -> Health:
        """Whether the source's connector answers, from the last minute unless ``fresh``."""
        cached = self._health.get(source.datasource_id)
        if (
            not fresh
            and cached is not None
            and cached[0] == source.connector_url
            and self._clock() - cached[1] < self._ttl
        ):
            return cached[2]
        try:
            await self.tools(tools, source, s)
        except ToolError:
            pass  # recorded by tools()
        return self._health[source.datasource_id][2]

    async def tools(self, tools: McpTools, source: DataSource, s: Settings) -> list[ToolInfo]:
        """The source's tools (``GET /datasources/{id}/tools``); whether they could be
        listed also becomes its health."""
        started = time.perf_counter()
        try:
            listed = await tools.list_tools(
                source.connector_url, timeout_seconds=s.connector_check_seconds
            )
        except ConnectorUnavailable as exc:
            _log_unavailable(source.connector_url, exc)
            problem = connection_problem(exc.reason, s.connector_check_seconds)
            self._keep(source, Health(False, None, None, exc.reason, problem.message))
            raise
        except ToolError as exc:
            log.info("connector of %s failed listing its tools: %r", source.datasource_id, exc)
            problem = connection_problem("not_mcp", s.connector_check_seconds)
            self._keep(source, Health(False, None, None, "not_mcp", problem.message))
            raise
        latency = round((time.perf_counter() - started) * 1000)
        self._keep(source, Health(True, latency, len(listed), None, None))
        return listed

    def remember(self, source: DataSource, result: TestResult) -> None:
        """Take a passed test of the source's address as its first health."""
        self._keep(source, Health(True, result.latency_ms, len(result.tools), None, None))

    def forget(self, datasource_id: str) -> None:
        self._health.pop(datasource_id, None)

    def _keep(self, source: DataSource, health: Health) -> None:
        self._health[source.datasource_id] = (source.connector_url, self._clock(), health)
