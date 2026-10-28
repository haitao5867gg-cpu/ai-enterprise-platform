"""Code nodes: checking their settings and running them in a (fake) sandbox."""

import traceback
from typing import Any

from mcp.server.mcpserver import MCPServer

from tests.conftest import SETTINGS
from tests.test_flow_tools import Platform


def sandbox() -> MCPServer:
    """Stands in for connectors/sandbox: same tool and answers, run in-process."""
    server = MCPServer("fake-sandbox")

    @server.tool()
    def run_python(code: str, inputs: dict[str, Any], timeout_seconds: int = 10) -> dict:
        """Run main(inputs)."""
        if "while True" in code:
            return {"status": "timeout", "error": f"运行超过 {timeout_seconds} 秒", "logs": ""}
        namespace: dict[str, Any] = {}
        try:
            exec(code, namespace)
            return {"status": "ok", "result": namespace["main"](inputs), "logs": ""}
        except Exception as exc:
            text = "".join(traceback.format_exception_only(exc)).strip()
            return {"status": "error", "error": text, "logs": "开始计算\n"}

    return server


SUM = """def main(inputs):
    numbers = [int(x) for x in inputs["raw"].split(",")]
    note = f"{{共}} {len(numbers)} 个"
    return {"total": sum(numbers), "note": note}
"""


def flow(code: str = SUM, on_error: dict | None = None) -> dict:
    calc: dict[str, Any] = {
        "id": "calc",
        "type": "code",
        "title": "求和",
        "code": code,
        "inputs": {"raw": "{{start.numbers}}", "unit": "元"},
        "outputs": [{"key": "total", "type": "number"}, {"key": "note", "type": "string"}],
        "timeout_seconds": 5,
    }
    outputs = ["{{calc.total}}"]
    if on_error:
        calc["on_error"] = on_error
        outputs.append("{{calc.error_type}}")
    return {
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "numbers", "label": "数字", "type": "text"}],
                },
                calc,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "total", "from": outputs}],
                },
            ],
            "edges": [{"from": "start", "to": "calc"}, {"from": "calc", "to": "end"}],
        }
    }


async def test_code_node_runs_in_the_sandbox():
    async with Platform(["x"], {SETTINGS.sandbox_url: sandbox()}) as p:
        await p.new_app("sum", flow())
        run = await p.run("sum", {"numbers": "1,2,3"})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"total": 6}
        view = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        assert {n["id"]: n["kind"] for n in view["nodes"]}["calc"] == "code"
        step = next(s for s in view["steps"] if s["node"] == "calc")
        assert step["output"]["vars"]["calc"] == {"total": 6, "note": "{共} 3 个"}

        failed = await p.run("sum", {"numbers": "1,x"})
        assert failed["status"] == "error"
        assert "ValueError: invalid literal" in failed["error"]
        assert "输出的最后几行：\n开始计算" in failed["error"]


async def test_code_failures_and_on_error():
    keep_going = {"then": "default", "default": {"total": 0, "note": ""}}
    async with Platform(["x"], {SETTINGS.sandbox_url: sandbox()}) as p:
        wrong = "def main(inputs):\n    return {'total': 'many', 'note': ''}\n"
        await p.new_app("wrong", flow(wrong, keep_going))
        assert (await p.run("wrong", {"numbers": "1"}))["result"] == {"total": 0}
        missing = "def main(inputs):\n    return {'total': 1}\n"
        await p.new_app("missing", flow(missing))
        run = await p.run("missing", {"numbers": "1"})
        assert run["error"] == "节点「求和」(calc)：main 返回的 dict 里没有 note"

        busy = "def main(inputs):\n    while True:\n        pass\n"
        await p.new_app("busy", flow(busy, {"then": "default", "default": {}}))
        run = await p.run("busy", {"numbers": "1"})
        assert run["result"] == {"total": "timeout"}

    async with Platform(["x"]) as p:  # No sandbox running.
        await p.new_app("sum", flow(on_error=keep_going))
        assert (await p.run("sum", {"numbers": "1"}))["result"] == {"total": 0}
        tried = await p.api.post(
            "/apps/sum/nodes/calc/test", json={"values": {"start.numbers": "1"}}
        )
        assert tried.json()["error"]["type"] == "sandbox_unavailable"


async def test_code_settings_are_checked():
    async with Platform(["x"]) as p:
        await p.api.post("/apps", json={"app": "sum", "title": "求和"})

        async def problems(**changes: Any) -> dict:
            config = flow()
            config["graph"]["nodes"][1].update(changes)
            resp = await p.api.put("/apps/sum/draft", json={"config": config})
            assert resp.status_code == 422, resp.text
            return {tuple(d["loc"][5:]): d["msg"] for d in resp.json()["detail"]}

        assert await problems(code="def main(inputs:\n") == {
            ("code",): "第 1 行语法错误：'(' was never closed"
        }
        assert await problems(code="def run(inputs):\n    return {}\n") == {
            ("code",): "代码里要定义 main(inputs) 函数"
        }
        assert await problems(timeout_seconds=31) == {("timeout_seconds",): "超时在 1–30 秒之间"}
        assert await problems(inputs={"Bad": 1}) == {
            ("inputs", "Bad"): "输入名只能用小写字母、数字和下划线，以字母开头"
        }
        assert await problems(outputs=[{"key": "total", "type": "int"}]) == {
            ("outputs", 0, "type"): "类型只能是 string、number、boolean、array、object"
        }
        assert await problems(inputs={"token": "{{secret.k}}"}) == {
            ("inputs", "token"): "密钥只能写在工具节点的参数里"
        }
        assert await problems(inputs={"x": "{{nope.text}}"}) == {("inputs", "x"): "没有节点 nope"}


async def test_trying_a_code_node_and_its_type():
    async with Platform(["x"], {SETTINGS.sandbox_url: sandbox()}) as p:
        await p.new_app("sum", flow())
        tried = await p.api.post(
            "/apps/sum/nodes/calc/test", json={"values": {"start.numbers": "4,5"}}
        )
        body = tried.json()
        assert (body["status"], body["outputs"]) == ("done", {"total": 9, "note": "{共} 2 个"})
        assert body["rendered"] == {"inputs.raw": "4,5"}
        types = {t["type"]: t for t in (await p.api.get("/node-types")).json()}
        assert types["code"]["error_handling"] is True
        assert types["code"]["defaults"]["timeout_seconds"] == 10
