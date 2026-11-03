"""Both executors against fake REST endpoints, and the cluster program run locally."""

import asyncio
import contextlib
import datetime
import io
import json
import types
from decimal import Decimal
from typing import Any

import httpx2 as httpx
import pytest

from aep_databricks import executor as ex
from aep_databricks.executor import (
    ClusterExecutor,
    Result,
    StatementError,
    WarehouseExecutor,
    make_executor,
    parse_output,
    program,
)
from aep_databricks.sql import ConnectorError
from tests.conftest import settings


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(ex, "POLL_SECONDS", 0)
    monkeypatch.setattr(ex, "START_POLL_SECONDS", 0)
    monkeypatch.setattr(ex, "RETRY_SECONDS", 0)


def client(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_the_executor_follows_the_settings():
    assert isinstance(make_executor(settings()), WarehouseExecutor)
    cluster = settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1")
    assert isinstance(make_executor(cluster), ClusterExecutor)
    with pytest.raises(ConnectorError, match="没有配置在哪里执行查询"):
        make_executor(settings(databricks_read_warehouse_id=""))


# SQL warehouse

SCHEMA = {
    "columns": [
        {"name": "id", "type_name": "LONG", "type_text": "BIGINT", "position": 0},
        {"name": "amount", "type_name": "DECIMAL", "type_text": "DECIMAL(18,2)", "position": 1},
        {"name": "units", "type_name": "DECIMAL", "type_text": "DECIMAL(10,0)", "position": 2},
        {"name": "paid", "type_name": "BOOLEAN", "type_text": "BOOLEAN", "position": 3},
        {"name": "tags", "type_name": "ARRAY", "type_text": "ARRAY<STRING>", "position": 4},
        {"name": "day", "type_name": "DATE", "type_text": "DATE", "position": 5},
        {"name": "ratio", "type_name": "DOUBLE", "type_text": "DOUBLE", "position": 6},
    ]
}


async def test_a_warehouse_statement_polls_and_reads_every_chunk():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/api/2.0/sql/statements":
            return httpx.Response(200, json={"statement_id": "s1", "status": {"state": "PENDING"}})
        if path == "/api/2.0/sql/statements/s1":
            return httpx.Response(
                200,
                json={
                    "statement_id": "s1",
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {"schema": SCHEMA, "total_chunk_count": 2},
                    "result": {
                        "data_array": [
                            ["1", "12.50", "3", "true", '["a","b"]', "2024-01-31", "NaN"]
                        ],
                        "next_chunk_internal_link": "/api/2.0/sql/statements/s1/result/chunks/1",
                    },
                },
            )
        if path == "/api/2.0/sql/statements/s1/result/chunks/1":
            return httpx.Response(
                200, json={"data_array": [["2", None, None, "false", None, None, "0.5"]]}
            )
        raise AssertionError(path)

    warehouse = WarehouseExecutor(settings(), client(handler))
    result = await warehouse.query("SELECT 1")
    assert result.columns[:3] == [
        ("id", "bigint"),
        ("amount", "decimal(18,2)"),
        ("units", "decimal(10,0)"),
    ]
    assert result.rows == [
        [1, 12.5, 3, True, ["a", "b"], "2024-01-31", "NaN"],
        [2, None, None, False, None, None, 0.5],
    ]
    sent = json.loads(seen[0].content)
    assert sent == {
        "statement": "SELECT 1",
        "warehouse_id": "wh1",
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    }
    assert seen[0].headers["authorization"] == "Bearer token-for-tests"


async def test_warehouse_failures_say_what_databricks_said():
    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "statement_id": "s1",
                "status": {
                    "state": "FAILED",
                    "error": {"message": "[UNRESOLVED_COLUMN] A column `Typ` cannot be resolved."},
                },
            },
        )

    with pytest.raises(StatementError, match=r"^Databricks 报错：\[UNRESOLVED_COLUMN\]"):
        await WarehouseExecutor(settings(), client(failed)).query("SELECT 1")

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Invalid access token"})

    with pytest.raises(ConnectorError, match="拒绝了令牌"):
        await WarehouseExecutor(settings(), client(refused)).query("SELECT 1")

    tries: list[int] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        tries.append(1)
        raise httpx.ConnectError("adb-1.test: name or service not known")

    with pytest.raises(ConnectorError) as found:
        await WarehouseExecutor(settings(), client(unreachable)).query("SELECT 1")
    assert "adb-1.test" not in str(found.value)
    assert len(tries) == ex.ATTEMPTS


async def test_dropped_connections_are_retried():
    dropped = iter([True, True, False])

    def flaky(request: httpx.Request) -> httpx.Response:
        if next(dropped):
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(
            200,
            json={
                "statement_id": "s1",
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "schema": {"columns": [{"name": "one", "type_name": "INT"}]},
                    "total_chunk_count": 1,
                },
                "result": {"data_array": [["1"]]},
            },
        )

    result = await WarehouseExecutor(settings(), client(flaky)).query("SELECT 1")
    assert result.rows == [[1]]


async def test_a_slow_warehouse_statement_is_cancelled(monkeypatch):
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(ex.time, "monotonic", lambda: next(clock))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"statement_id": "s1", "status": {"state": "RUNNING"}})

    with pytest.raises(ConnectorError, match="超过 150 秒没有结束，已取消"):
        await WarehouseExecutor(settings(), client(handler)).query("SELECT 1")
    assert calls[-1] == "POST /api/2.0/sql/statements/s1/cancel"


# Cluster


class Cluster:
    """The Clusters and Command Execution endpoints, answering with a program's output."""

    def __init__(
        self,
        state: str = "RUNNING",
        output: str = "",
        error: str = "",
        after_start: list[str] | None = None,
    ) -> None:
        self.state = state
        self.output = output
        self.error = error
        self.commands: list[dict[str, Any]] = []
        self.contexts = 0
        # The states a started cluster goes through: the first right after the start, then
        # one per look; the last one stays.
        self.after_start = after_start or ["PENDING", "RUNNING"]
        self.starts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/2.0/clusters/get":
            assert request.url.params["cluster_id"] == "c1"
            state = self.state
            if self.starts and self.after_start:
                self.state = self.after_start.pop(0)
                state = self.state
            body = {"cluster_id": "c1", "state": state}
            if state == "TERMINATED" and self.starts:
                body["state_message"] = "配额不够"
            return httpx.Response(200, json=body)
        if path == "/api/2.0/clusters/start":
            assert json.loads(request.content) == {"cluster_id": "c1"}
            assert self.state == "TERMINATED"
            self.starts += 1
            self.state = self.after_start.pop(0)
            return httpx.Response(200, json={})
        if path == "/api/1.2/contexts/create":
            self.contexts += 1
            return httpx.Response(200, json={"id": f"ctx{self.contexts}"})
        if path == "/api/1.2/contexts/status":
            return httpx.Response(200, json={"status": "Running"})
        if path == "/api/1.2/commands/execute":
            self.commands.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "cmd1"})
        if path == "/api/1.2/commands/status":
            if self.error:
                results = {"resultType": "error", "cause": self.error}
                return httpx.Response(200, json={"status": "Finished", "results": results})
            results = {"resultType": "text", "data": self.output}
            return httpx.Response(200, json={"status": "Finished", "results": results})
        if path == "/api/1.2/contexts/destroy":
            return httpx.Response(200, json={})
        raise AssertionError(path)


def output(columns: list[list[str]], rows: list[list[Any]]) -> str:
    text = json.dumps({"columns": columns, "rows": rows})
    return f"{ex.MARK} {len(text)} {text}\n"


async def test_cluster_statements_run_in_one_reused_context():
    cluster = Cluster(output=output([["n", "bigint"]], [[1]]))
    executor = ClusterExecutor(
        settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1"), client(cluster)
    )
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert await executor.query("SELECT 'it''s' AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.contexts == 1
    sent = cluster.commands[1]
    assert sent["language"] == "python" and sent["contextId"] == "ctx1"
    compile(sent["command"], "<program>", "exec")  # The statement is a literal, not code.
    assert json.dumps("SELECT 'it''s' AS n") in sent["command"]
    await executor.close()


async def test_cluster_statements_run_side_by_side_in_a_few_contexts():
    """Up to AEP_DATABRICKS_READ_CONTEXTS statements at once, each in its own context
    (jadeflow retest S-18: 48 checks one after another took 4 minutes)."""
    cluster = Cluster(output=output([["n", "bigint"]], [[1]]))
    busy: dict[str, int] = {}
    seen: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        found = cluster(request)
        if request.url.path == "/api/1.2/commands/execute":
            context = json.loads(request.content)["contextId"]
            busy[context] = busy.get(context, 0) + 1
            seen.append(sum(busy.values()))
            await asyncio.sleep(0.01)
        elif request.url.path == "/api/1.2/commands/status":
            context = request.url.params["contextId"]
            busy[context] -= 1
        return found

    three = settings(
        databricks_read_warehouse_id="", databricks_read_cluster_id="c1", databricks_read_contexts=3
    )
    executor = ClusterExecutor(three, client(handler))
    found = await asyncio.gather(*(executor.query(f"SELECT {i} AS n") for i in range(7)))
    assert found == [Result([("n", "bigint")], [[1]])] * 7
    assert cluster.contexts == 3 and max(seen) == 3
    assert all(n <= 1 for n in busy.values())  # one statement at a time in a context
    # Idle contexts are reused.
    await executor.query("SELECT 1 AS n")
    assert cluster.contexts == 3
    await executor.close()


async def test_a_dropped_context_is_made_again_once():
    cluster = Cluster(output=output([["n", "bigint"]], [[1]]))
    gone = {"ctx1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.2/commands/execute":
            if json.loads(request.content)["contextId"] in gone:
                return httpx.Response(400, json={"error": "Context ctx1 not found"})
        return cluster(request)

    executor = ClusterExecutor(
        settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1"), client(handler)
    )
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.contexts == 2
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.contexts == 2  # the new one is kept
    await executor.close()


async def test_a_restart_drops_every_idle_context():
    """After a cluster restart every pooled context is gone: the next statement must not
    try them one by one (the probe answered 500 Internal Server Error on 2026-11-03)."""
    cluster = Cluster(output=output([["n", "bigint"]], [[1]]))
    gone: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.2/commands/execute":
            context = json.loads(request.content)["contextId"]
            if context in gone:
                return httpx.Response(400, json={"error": f"Context {context} not found"})
            await asyncio.sleep(0.01)
        return cluster(request)

    three = settings(
        databricks_read_warehouse_id="", databricks_read_cluster_id="c1", databricks_read_contexts=3
    )
    executor = ClusterExecutor(three, client(handler))
    await asyncio.gather(*(executor.query(f"SELECT {i} AS n") for i in range(3)))
    assert cluster.contexts == 3
    gone |= {"ctx1", "ctx2", "ctx3"}  # the cluster restarted
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.contexts == 4
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.contexts == 4  # the new one is kept, the dead ones are not tried again
    await executor.close()


async def test_a_context_that_vanishes_twice_is_a_connector_error():
    cluster = Cluster(output=output([["n", "bigint"]], [[1]]))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.2/commands/execute":
            return httpx.Response(400, json={"error": "Context not found"})
        return cluster(request)

    executor = ClusterExecutor(
        settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1"), client(handler)
    )
    with pytest.raises(ConnectorError, match="集群可能正在重启"):
        await executor.query("SELECT 1 AS n")
    assert cluster.contexts == 2
    await executor.close()


async def test_a_stopped_cluster_is_not_started():
    cluster = Cluster(state="TERMINATED")
    executor = ClusterExecutor(
        settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1"), client(cluster)
    )
    with pytest.raises(ConnectorError, match="集群 c1 没有在运行，请先在 Databricks 上启动它"):
        await executor.query("SELECT 1")
    assert cluster.contexts == 0


async def test_a_stopped_cluster_is_started_when_the_setting_is_on():
    cluster = Cluster(
        state="TERMINATED",
        output=output([["n", "bigint"]], [[1]]),
        after_start=["PENDING", "PENDING", "RUNNING"],
    )
    on = settings(
        databricks_read_warehouse_id="",
        databricks_read_cluster_id="c1",
        databricks_read_start_cluster=True,
    )
    executor = ClusterExecutor(on, client(cluster))
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.starts == 1 and cluster.state == "RUNNING"
    # Running now: the next statement does not start it again.
    assert await executor.query("SELECT 1 AS n") == Result([("n", "bigint")], [[1]])
    assert cluster.starts == 1
    await executor.close()


async def test_a_cluster_that_is_already_starting_is_waited_for():
    cluster = Cluster(state="PENDING", output=output([["n", "bigint"]], [[1]]))
    states = iter(["PENDING", "RUNNING"])
    real = cluster.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/clusters/get" and cluster.state == "PENDING":
            cluster.state = next(states)
        return real(request)

    on = settings(
        databricks_read_warehouse_id="",
        databricks_read_cluster_id="c1",
        databricks_read_start_cluster=True,
    )
    executor = ClusterExecutor(on, client(handler))
    assert await executor.query("SELECT 1") == Result([("n", "bigint")], [[1]])
    assert cluster.starts == 0
    await executor.close()


async def test_a_cluster_that_fails_to_start_says_why():
    cluster = Cluster(state="TERMINATED", after_start=["PENDING", "TERMINATED"])
    on = settings(
        databricks_read_warehouse_id="",
        databricks_read_cluster_id="c1",
        databricks_read_start_cluster=True,
    )
    executor = ClusterExecutor(on, client(cluster))
    with pytest.raises(ConnectorError, match="集群 c1 没能启动：配额不够"):
        await executor.query("SELECT 1")
    assert cluster.starts == 1 and cluster.contexts == 0


async def test_a_cluster_still_starting_at_the_timeout_says_to_retry(monkeypatch):
    cluster = Cluster(state="TERMINATED", after_start=["PENDING"])
    on = settings(
        databricks_read_warehouse_id="",
        databricks_read_cluster_id="c1",
        databricks_read_start_cluster=True,
        databricks_read_timeout_seconds=5,
    )
    executor = ClusterExecutor(on, client(cluster))
    clock = iter(range(0, 1000, 3))
    # Only the executor's clock: the event loop keeps the real one.
    monkeypatch.setattr(ex, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(ConnectorError, match="集群 c1 正在启动（PENDING），等了 5 秒还没就绪"):
        await executor.query("SELECT 1")
    assert cluster.starts == 1


async def test_a_cluster_error_gives_the_error_line_not_the_traceback():
    trace = (
        "---------------------------------------------------------------------------\n"
        "AnalysisException                         Traceback (most recent call last)\n"
        '      1 _aep_df = spark.sql("SELECT * FROM main.s.nope")\n'
        "AnalysisException: [TABLE_OR_VIEW_NOT_FOUND] The table or view `main`.`s`.`nope` "
        "cannot be found.\n"
    )
    executor = ClusterExecutor(
        settings(databricks_read_warehouse_id="", databricks_read_cluster_id="c1"),
        client(Cluster(error=trace)),
    )
    with pytest.raises(StatementError) as found:
        await executor.query("SELECT * FROM main.s.nope")
    assert str(found.value) == (
        "Databricks 报错：AnalysisException: [TABLE_OR_VIEW_NOT_FOUND] The table or view "
        "`main`.`s`.`nope` cannot be found."
    )


class _Field:
    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.dataType = type("T", (), {"simpleString": lambda _self: kind})()


class _Row(tuple):
    def asDict(self, recursive: bool = False) -> dict[str, Any]:  # noqa: N802 - Spark's name
        return {"city": self[0], "n": self[1]}


class _Frame:
    def __init__(self, statement: str) -> None:
        self.statement = statement
        self.schema = type("S", (), {"fields": [_Field("v", "decimal(18,2)"), _Field("x", "x")]})()

    def collect(self) -> list[tuple[Any, ...]]:
        return [
            (Decimal("12.50"), datetime.date(2024, 1, 31)),
            (Decimal("7"), datetime.datetime(2024, 1, 31, 8, 30)),
            (Decimal("NaN"), b"\x00\x01"),
            (float("nan"), {"k": [1, Decimal("2.5")]}),
            (None, _Row(("上海", 3))),
        ]


def test_the_cluster_program_converts_spark_values_to_json():
    ran: list[str] = []

    class Spark:
        def sql(self, statement: str) -> _Frame:
            ran.append(statement)
            return _Frame(statement)

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        exec(program("SELECT \"a\" || '\\'' AS v"), {"spark": Spark()})  # noqa: S102
    assert ran == ["SELECT \"a\" || '\\'' AS v"]
    result = parse_output(printed.getvalue())
    assert result.columns == [("v", "decimal(18,2)"), ("x", "x")]
    assert result.rows == [
        [12.5, "2024-01-31"],
        [7, "2024-01-31T08:30:00"],
        ["NaN", "AAE="],
        ["nan", {"k": [1, 2.5]}],
        [None, {"city": "上海", "n": 3}],
    ]


def test_cut_off_cluster_output_is_reported():
    whole = output([["n", "bigint"]], [[1], [2]])
    assert parse_output("noise\n" + whole).rows == [[1], [2]]
    with pytest.raises(ConnectorError, match="被集群截断"):
        parse_output(whole[:-10])
    with pytest.raises(ConnectorError, match="被集群截断"):
        parse_output(whole[:-10] + "*** WARNING: max output size exceeded, skipping output. ***")
    with pytest.raises(ConnectorError, match="没有返回结果"):
        parse_output("")
