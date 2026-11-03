"""Error messages in the reader's language (aep.i18n)."""

import re

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from aep.api.validation import chinese_errors
from aep.i18n import language, translate, translated_http_error
from aep.main import create_app
from aep.messages_en import MESSAGES


def test_language_follows_accept_language() -> None:
    assert language(None) == "zh"
    assert language("") == "zh"
    assert language("en") == "en"
    assert language("en-US,en;q=0.9") == "en"
    assert language("zh-CN,zh;q=0.9,en;q=0.8") == "zh"
    assert language("en;q=0.5,zh;q=0.9") == "zh"
    assert language("fr,en;q=0.7") == "en"
    assert language("fr") == "zh"
    assert language("en;q=0") == "zh"
    assert language("en;q=bad") == "zh"


def test_translate_whole_messages_templates_and_parts() -> None:
    assert translate("请先登录", "zh") == "请先登录"
    assert translate("请先登录", "en") == "Sign in first"
    assert translate("没有应用 orders", "en") == "There is no app orders"
    # The more specific template wins.
    assert translate("没有应用「订单」", "en") == "There is no app “订单”"
    assert translate("应用 a 没有版本 3", "en") == "App a has no version 3"
    # A value that is itself a message (a role) is translated too.
    assert translate("没有权限：这项操作需要审批者", "en") == (
        "Not allowed: this needs the approver role"
    )
    # Joined messages are translated part by part; unknown parts stay as they are.
    assert translate("文件为空；不认识的话", "en") == "The file is empty; 不认识的话"
    # A message the catalog lacks is returned in Chinese.
    assert translate("某个新的报错", "en") == "某个新的报错"


def test_catalog_english_uses_the_values_its_message_carries() -> None:
    placeholder = re.compile(r"\{(\w+)\}")
    for chinese, english in MESSAGES.items():
        names = placeholder.findall(chinese)
        assert len(names) == len(set(names)), chinese
        assert set(placeholder.findall(english)) <= set(names), chinese


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, translated_http_error)
    router = APIRouter(route_class=chinese_errors({"name": "名称"}))

    class Body(BaseModel):
        name: str = Field(min_length=1)

    @router.get("/apps/{app}")
    async def missing(app: str) -> None:
        raise HTTPException(404, f"没有应用 {app}")

    @router.post("/things")
    async def create(body: Body) -> None:
        raise HTTPException(409, "名称不能为空")

    app.include_router(router)
    return app


def test_errors_follow_the_request_language() -> None:
    client = TestClient(_app())
    assert client.get("/apps/x").json() == {"detail": "没有应用 x"}
    english = {"Accept-Language": "en"}
    assert client.get("/apps/x", headers=english).json() == {"detail": "There is no app x"}
    # Validation errors in the shared route class come out in English too.
    response = client.post("/things", json={"name": ""}, headers=english)
    assert response.status_code == 422
    assert response.json() == {"detail": "“名称” has the wrong format, length or range"}
    # Unknown routes keep FastAPI's own English.
    assert client.get("/nowhere", headers=english).json() == {"detail": "Not Found"}


def test_the_platform_answers_in_english() -> None:
    app = create_app()
    app.state.ops = object()  # the check below answers before the service is used
    client = TestClient(app)
    response = client.get("/ops/summary?days=5", headers={"Accept-Language": "en"})
    assert response.status_code == 422
    assert response.json() == {"detail": "days must be 7, 30 or 90"}
    response = client.get("/ops/summary?days=5", headers={"Accept-Language": "zh-CN"})
    assert response.json() == {"detail": "days 只能是 7、30 或 90"}


def test_node_types_in_english() -> None:
    client = TestClient(create_app())
    chinese = client.get("/node-types").json()
    english = client.get("/node-types", headers={"Accept-Language": "en"}).json()
    llm = next(item for item in english if item["type"] == "llm")
    assert llm["label"] == "Call model"
    assert llm["description"] == "Call a model once for a text or a few fields"
    assert llm["presets"][0]["label"] == "Extract fields"
    # Settings, prompts included, are the same in both languages.
    assert [item["defaults"] for item in english] == [item["defaults"] for item in chinese]
    # Every name and description the node types show has its English.
    cjk = re.compile(r"[一-鿿]")
    for item in english:
        texts = [item["label"], item["description"]]
        texts += [output["description"] for output in item["outputs"]]
        texts += [p[key] for p in item["presets"] for key in ("label", "description")]
        assert not [text for text in texts if cjk.search(text)], item["type"]
