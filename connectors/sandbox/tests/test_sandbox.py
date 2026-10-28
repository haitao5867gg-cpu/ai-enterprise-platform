import os
import time

import pytest
from mcp import Client

from aep_sandbox.config import Settings
from aep_sandbox.runner import Runner
from aep_sandbox.server import build

ROOT = os.geteuid() == 0


@pytest.fixture
def runner(tmp_path) -> Runner:
    return Runner(Settings(sandbox_work_dir=tmp_path, sandbox_max_output_bytes=100_000))


async def test_pandas_result_and_logs(runner):
    code = """
import pandas as pd
import numpy as np

def main(inputs):
    df = pd.DataFrame(inputs["rows"])
    print("rows:", len(df))
    return {
        "total": df["amount"].sum(),
        "mean": np.float64("nan"),
        "by_city": df.groupby("city")["amount"].sum().to_dict(),
        "top": df.nlargest(1, "amount"),
    }
"""
    rows = [{"city": "南京", "amount": 3}, {"city": "苏州", "amount": 4.5}]
    reply = await runner.run(code, {"rows": rows}, 20)
    assert reply == {
        "status": "ok",
        "result": {
            "total": 7.5,
            "mean": None,
            "by_city": {"南京": 3.0, "苏州": 4.5},
            "top": [{"city": "苏州", "amount": 4.5}],
        },
        "logs": "rows: 2\n",
    }


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("def main(inputs):\n    return {", "第 2 行语法错误"),
        ("x = 1\n", "代码里要定义 main(inputs) 函数"),
        ("def main(inputs):\n    return 1 / 0\n", "第 2 行，在 main 里\nZeroDivisionError"),
        ("def main(inputs):\n    return {'a': object()}\n", "不能转成 JSON 的 object"),
        ("def main(inputs):\n    return 'x' * 200_000\n", "返回值超过"),
        ("def main(inputs):\n    return bytearray(2 << 30)\n", "内存不够"),
        (
            "def main(inputs):\n    open('big', 'wb').write(b'x' * (32 << 20))\n",
            "File too large",
        ),
    ],
)
async def test_errors(runner, code, error):
    reply = await runner.run(code, {}, 10)
    assert reply["status"] == "error"
    assert error in reply["error"]


async def test_time_limits(runner):
    started = time.monotonic()
    busy = await runner.run("def main(inputs):\n    while True:\n        pass\n", {}, 1)
    assert busy == {"status": "timeout", "error": "运行超过 1 秒", "logs": ""}
    sleeping = await runner.run("import time\ndef main(inputs):\n    time.sleep(60)\n", {}, 1)
    assert sleeping["status"] == "timeout"
    assert time.monotonic() - started < 10


async def test_nothing_from_the_server_leaks_in(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("AEP_DATABASE_URL", "postgresql://secret")
    code = """
import os, sys
def main(inputs):
    open("note.txt", "w").write("x")
    return {"env": sorted(os.environ), "cwd": os.getcwd(), "path": sys.path}
"""
    reply = await runner.run(code, {}, 10)
    assert reply["status"] == "ok"
    env = reply["result"]["env"]
    assert not [k for k in env if k.startswith("AEP_")]
    assert reply["result"]["cwd"].startswith(str(tmp_path))
    assert reply["result"]["cwd"] not in reply["result"]["path"]
    assert list(tmp_path.iterdir()) == []  # The work directory is gone.


@pytest.mark.skipif(ROOT, reason="process and file limits do not bind root")
async def test_no_processes_and_no_writes_outside(runner):
    spawn = "import subprocess\ndef main(inputs):\n    subprocess.run(['true'])\n"
    assert (await runner.run(spawn, {}, 10))["status"] == "error"
    write = "def main(inputs):\n    open('/etc/aep-sandbox', 'w')\n"
    assert "Permission denied" in (await runner.run(write, {}, 10))["error"]


async def test_over_mcp(tmp_path):
    async with Client(build(Settings(sandbox_work_dir=tmp_path))) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["run_python"]
        result = await client.call_tool(
            "run_python",
            {"code": "def main(inputs):\n    return {'n': inputs['n'] + 1}", "inputs": {"n": 1}},
        )
        assert result.structured_content == {"status": "ok", "result": {"n": 2}, "logs": ""}
        bad = await client.call_tool("run_python", {"code": "", "inputs": {}})
        assert bad.is_error


async def test_relay_forwards_to_the_sandbox():
    import asyncio
    import socket

    from aep_sandbox.relay import serve

    async def echo(reader, writer):
        writer.write(b"sandbox:" + await reader.readline())
        await writer.drain()
        writer.close()

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    target = upstream.sockets[0].getsockname()[1]
    port = free_port()
    settings = Settings(
        sandbox_mcp_host="127.0.0.1",
        sandbox_relay_port=port,
        sandbox_relay_target_host="127.0.0.1",
        sandbox_relay_target_port=target,
    )
    task = asyncio.create_task(serve(settings))
    try:
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        writer.write(b"hello\n")
        await writer.drain()
        assert await reader.read() == b"sandbox:hello\n"
        writer.close()
    finally:
        task.cancel()
        upstream.close()
