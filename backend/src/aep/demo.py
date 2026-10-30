"""Prepares the ETL demo on a running platform: ``aep-demo`` (``make demo``).

Through the platform's HTTP API only (docs/api/data-nodes.md, "演示"):

1. checks (``GET /health/demo``): the model alias, the embedding model, the sample
   data source with its ``retail`` database, the Databricks workspace; Databricks
   is only warned about;
2. the two knowledge bases, from the Markdown files in ``demo/knowledge/<dir>``:
   made when missing (with the checked embedding model), a file uploaded when its
   document is missing and replaced when the content differs, documents without a
   file left alone; a base on another embedding model is switched when it holds
   only the demo's documents (they are uploaded again); then waits until every
   file's document is processed;
3. the app ``retail-etl`` from the template ``etl-lakehouse``, with the bases, the
   model alias and the start node's defaults filled in, published when its current
   version differs from that;
4. prints the pages to open and the demo request.

Running it again changes nothing. The MySQL ``retail`` database itself is loaded by
the Makefile before this runs (``make demo``).
"""

import argparse
import asyncio
import copy
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aep.health import DEMO_DATASOURCE, DEMO_SCHEMA, find_demo_source

APP = "retail-etl"
APP_TITLE = "零售数据 ETL"
TEMPLATE = "etl-lakehouse"
TABLES = f"{DEMO_SCHEMA}.*"
REQUEST = (
    "按月、按城市统计有效订单的销售额、订单数、下单客户数和客单价，再按会员等级拆开。"
    "口径按公司数据口径执行。"
)
SPARE_REQUEST = "统计每个品类每月的销售额和毛利率，只看线上渠道。"
ACTOR = "aep-demo"
KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "demo" / "knowledge"
WAIT_SECONDS = 300.0
POLL_SECONDS = 3.0


@dataclass(frozen=True)
class Base:
    folder: str
    name: str
    description: str
    node: str  # the template's knowledge node that searches it


BASES = (
    Base(
        "retail-metrics",
        "零售数据口径",
        "零售业务的指标定义和统计口径：有效订单、销售额、客单价、城市和会员等级的归一、"
        "时间口径、客户数和复购、毛利",
        "glossary",
    ),
    Base(
        "dw-standards",
        "数仓开发规范",
        "数据平台团队的数仓开发规范：分层和命名、主键和字段类型、数据质量规则、加载策略、"
        "PySpark 写法",
        "standards",
    ),
)


class DemoError(Exception):
    """A step that cannot go on; the message says what to do."""


def files_of(folder: Path) -> dict[str, bytes]:
    """The documents of a base: the Markdown files of ``folder`` but its README."""
    return {
        p.name: p.read_bytes()
        for p in sorted(folder.glob("*.md"))
        if p.is_file() and p.name.lower() != "readme.md"
    }


class Demo:
    def __init__(
        self,
        api: httpx.AsyncClient,
        web: str = "http://localhost:3000",
        knowledge_dir: Path = KNOWLEDGE_DIR,
        out: Callable[[str], None] = print,
        wait_seconds: float = WAIT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.web = web.rstrip("/")
        self.knowledge_dir = knowledge_dir
        self.out = out
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep

    async def run(self) -> bool:
        """Every step in order; False when one could not be done."""
        try:
            self.out("1. 检查")
            model, embedding = await self.check()
            self.out("2. 知识库")
            bases = {base.node: await self.base(base, embedding) for base in BASES}
            await self.wait_ready(bases)
            self.out("3. 应用")
            await self.app(model, bases)
        except DemoError as exc:
            self.out(f"  ✗ {exc}")
            return False
        self.out("4. 完成，演示时打开：")
        self.out(f"  应用：{self.web}/apps/{APP}")
        for base in BASES:
            self.out(f"  知识库「{base.name}」：{self.web}/knowledge/{bases[base.node]['kb_id']}")
        self.out(f"  演示需求（开始运行时已经填好）：{REQUEST}")
        self.out(f"  备用需求：{SPARE_REQUEST}")
        return True

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.api.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DemoError(f"连不上平台接口 {self.api.base_url}：{exc!r}；先 make up") from exc

    async def _json(self, method: str, path: str, ok: int = 200, **kwargs: Any) -> Any:
        resp = await self._call(method, path, **kwargs)
        if resp.status_code != ok:
            raise DemoError(f"{method} {path} 失败：HTTP {resp.status_code} {resp.text[:500]}")
        return resp.json() if resp.content else None

    # 1. checks

    async def check(self) -> tuple[str, str | None]:
        """Prints the platform's demo checks; the model alias the app is to use and
        the bases' embedding model (None: the platform's default)."""
        resp = await self._call("GET", "/health/demo")
        if resp.status_code == 404:
            raise DemoError("平台没有 /health/demo 接口，版本太旧：先 make up 重新构建")
        if resp.status_code != 200:
            raise DemoError(f"检查失败：HTTP {resp.status_code} {resp.text[:500]}")
        items = resp.json()
        for item in items:
            mark = "✓" if item["ok"] else ("✗" if item.get("required", True) else "!")
            self.out(f"  {mark} {item['detail']}")
        failed = [i for i in items if not i["ok"] and i.get("required", True)]
        if failed:
            raise DemoError("上面打 ✗ 的项是必需的，解决后再运行 make demo")
        values = {i["key"]: i.get("value") for i in items}
        if not values.get("model"):
            raise DemoError("检查结果里没有模型别名")
        return values["model"], values.get("embedding")

    # 2. knowledge bases

    async def base(self, base: Base, embedding: str | None = None) -> dict[str, Any]:
        """The base, made when missing, with its documents in line with its files."""
        files = files_of(self.knowledge_dir / base.folder)
        if not files:
            raise DemoError(f"{self.knowledge_dir / base.folder} 里没有 Markdown 文档")
        found = [b for b in await self._json("GET", "/knowledge-bases") if b["name"] == base.name]
        made = not found
        switched = None
        if made:
            body = {"name": base.name, "description": base.description}
            if embedding:
                body["embedding_model"] = embedding
            kb = await self._json("POST", "/knowledge-bases", 201, json=body)
        else:
            kb = found[0]
            changes: dict[str, Any] = {}
            if kb["description"] != base.description:
                changes["description"] = base.description
            if embedding and kb["embedding_model"] != embedding:
                switched = kb["embedding_model"]
                await self._clear(kb, files, embedding)
                changes["embedding_model"] = embedding
            if changes:
                kb = await self._json("PATCH", f"/knowledge-bases/{kb['kb_id']}", json=changes)
        path = f"/knowledge-bases/{kb['kb_id']}/documents"
        documents = await self._json("GET", path)
        upload: dict[str, bytes] = {}
        replaced = reindexed = 0
        for name, content in files.items():
            same = [d for d in documents if d["name"] == name]
            keep = None
            for doc in same:
                if keep is None and await self._same(path, doc, content):
                    keep = doc
                else:
                    await self._json("DELETE", f"{path}/{doc['document_id']}", 204)
            if keep is None:
                upload[name] = content
                replaced += bool(same)
            elif keep["status"] == "error":
                await self._json("POST", f"{path}/{keep['document_id']}/reindex", 202)
                reindexed += 1
        if upload:
            sent = [("files", (name, content, "text/markdown")) for name, content in upload.items()]
            await self._json("POST", path, 202, files=sent)
        done = []
        if made:
            done.append("新建")
        if switched:
            done.append(f"向量模型从 {switched} 换成 {embedding}")
        if len(upload) > replaced:
            done.append(f"上传 {len(upload) - replaced} 篇")
        if replaced:
            done.append(f"更新 {replaced} 篇")
        if reindexed:
            done.append(f"重新处理 {reindexed} 篇出错的")
        said = "、".join(done) if done else "已是最新"
        self.out(
            f"  ✓ {base.name}：{said}（{len(files)} 篇文档，向量模型 {kb['embedding_model']}）"
        )
        return kb | {"files": sorted(files)}

    async def _clear(self, kb: dict[str, Any], files: dict[str, bytes], embedding: str) -> None:
        """Deletes the base's documents so its embedding model can change; only when
        each is one of the demo's files, which are uploaded again."""
        path = f"/knowledge-bases/{kb['kb_id']}/documents"
        documents = await self._json("GET", path)
        others = sorted({d["name"] for d in documents} - set(files))
        if others:
            raise DemoError(
                f"知识库「{kb['name']}」用的向量模型是 {kb['embedding_model']}，演示要用 "
                f"{embedding}；换模型要先删掉全部文档，但里面有不是演示的文档："
                f"{'、'.join(others)}。删掉它们，或者设 AEP_DEMO_EMBEDDING_MODEL="
                f"{kb['embedding_model']}，再运行 make demo"
            )
        for doc in documents:
            await self._json("DELETE", f"{path}/{doc['document_id']}", 204)

    async def _same(self, path: str, doc: dict[str, Any], content: bytes) -> bool:
        if doc["size_bytes"] != len(content):
            return False
        resp = await self._call("GET", f"{path}/{doc['document_id']}/file")
        return resp.status_code == 200 and resp.content == content

    async def wait_ready(self, bases: dict[str, dict[str, Any]]) -> None:
        """Waits until the document of every file is processed."""
        deadline = time.monotonic() + self.wait_seconds
        said = False
        while True:
            pending: list[str] = []
            for kb in bases.values():
                path = f"/knowledge-bases/{kb['kb_id']}/documents"
                docs = {d["name"]: d for d in await self._json("GET", path)}
                for name in kb["files"]:
                    doc = docs.get(name)
                    if doc is None:
                        raise DemoError(f"知识库「{kb['name']}」里没有 {name}，上传没有成功")
                    if doc["status"] == "error":
                        raise DemoError(
                            f"知识库「{kb['name']}」的 {name} 处理出错：{doc.get('error')}；"
                            "检查向量模型（模型网关）后再运行 make demo"
                        )
                    if doc["status"] != "ready":
                        pending.append(name)
            if not pending:
                total = sum(len(kb["files"]) for kb in bases.values())
                self.out(f"  ✓ {total} 篇文档都已处理完成，可以检索")
                return
            if time.monotonic() > deadline:
                raise DemoError(
                    f"等了 {self.wait_seconds:g} 秒，还有 {len(pending)} 篇文档没处理完："
                    + "、".join(pending)
                )
            if not said:
                self.out(f"  … 等待 {len(pending)} 篇文档处理完成（切片、生成向量）")
                said = True
            await self.sleep(self.poll_seconds)

    # 3. the app

    async def app(self, model: str, bases: dict[str, dict[str, Any]]) -> None:
        resp = await self._call("GET", f"/apps/{APP}")
        created = resp.status_code == 404
        if created:
            body = {"app": APP, "title": APP_TITLE, "from": {"template": TEMPLATE}}
            detail = await self._json("POST", "/apps", 201, json=body)
            self.out(f"  ✓ 用模板 {TEMPLATE} 新建应用「{APP_TITLE}」（{APP}）")
        elif resp.status_code == 200:
            detail = resp.json()
        else:
            raise DemoError(f"GET /apps/{APP} 失败：HTTP {resp.status_code} {resp.text[:500]}")
        if detail.get("kind") != "flow":
            raise DemoError(f"已经有一个叫 {APP} 的代码应用，不能用作演示应用")
        # A new app from a template that needs bases has the template in its draft.
        had_draft = detail.get("draft") is not None and not created
        config = await self.config(detail, model, bases)
        saved = await self._json("PUT", f"/apps/{APP}/draft", json={"config": config})
        current = saved["current"]
        if saved["draft"]["config"] == current["config"]:
            await self._json("DELETE", f"/apps/{APP}/draft", 204)
            note = "；原来的草稿已丢弃" if had_draft else ""
            self.out(f"  ✓ 应用「{APP_TITLE}」已是最新（版本 {current['version']}）{note}")
            return
        body = {"note": f"make demo：按模板 {TEMPLATE} 配置", "actor": ACTOR}
        published = await self._json("POST", f"/apps/{APP}/draft/publish", json=body)
        note = "；覆盖了原来的草稿" if had_draft else ""
        version = published["current"]["version"]
        self.out(f"  ✓ 应用「{APP_TITLE}」发布了版本 {version}{note}")

    async def config(
        self, detail: dict[str, Any], model: str, bases: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """The template's configuration with the demo's choices filled in."""
        templates = await self._json("GET", "/app-templates")
        template = next((t for t in templates if t["id"] == TEMPLATE), None)
        if template is None:
            raise DemoError(f"平台没有模板 {TEMPLATE}，版本太旧：先 make up 重新构建")
        sources = await self._json("GET", "/datasources")
        # The demo runs outside the platform, so it looks for the local stack's connector.
        source = find_demo_source(sources, lambda d: (d["name"], d["kind"], d["connector_url"]))
        if source is None:
            raise DemoError(f"没有登记数据源「{DEMO_DATASOURCE}」")
        published = {s["name"]: s["published_version"] for s in detail["options"]["skills"]}
        config = copy.deepcopy(template["config"])
        defaults = {"datasource": source["datasource_id"], "tables": TABLES, "request": REQUEST}
        for node in config["graph"]["nodes"]:
            if node["type"] == "start":
                for field in node["inputs"]:
                    if field["key"] in defaults:
                        field["default"] = defaults[field["key"]]
            if node["id"] in bases:
                node["knowledge_bases"] = [bases[node["id"]]["kb_id"]]
            if node["type"] == "llm":
                node["model"] = model
                node["skills"] = [
                    s | {"version": published.get(s["name"]) or s["version"]}
                    for s in node.get("skills") or []
                ]
        return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aep-demo", description="准备 ETL 演示环境")
    parser.add_argument("--api", default="http://localhost:8000", help="平台 API 地址")
    parser.add_argument("--web", default="http://localhost:3000", help="平台页面地址")
    parser.add_argument(
        "--knowledge", type=Path, default=KNOWLEDGE_DIR, help="知识库文档目录（demo/knowledge）"
    )
    args = parser.parse_args(argv)
    print(f"准备 ETL 演示（平台 {args.api}）")

    async def go() -> bool:
        async with httpx.AsyncClient(base_url=args.api, timeout=60) as api:
            return await Demo(api, args.web, args.knowledge).run()

    return 0 if asyncio.run(go()) else 1


if __name__ == "__main__":
    sys.exit(main())
